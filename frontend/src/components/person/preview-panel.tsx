// 效果预览三档（批次 D）：props 驱动，可挂进创建弹窗，不依赖旧 workspace store。
//  ① 分镜预览：逐镜卡片（旁白/候选缩略图/相关性/来源授权），顶部三数指标（相关/兜底/缺失），
//     缺素材/低相关醒目警告；秒级返回、不渲染。无结构化分镜时后端确定性派生预览镜（不依赖 LLM）。
//  ② 试听：所选音色与语速合成前约 15 秒（后端裁文本 → 复用 /tts/preview）。
//  ③ 样片：以当前参数提交一支截断的短任务（快路径），返回后按普通任务轮询。
// 参数签名变更后把上次结果标为「已过期」，后端按参数哈希缓存（命中新键即重算）。
import { For, Show, createMemo, createSignal } from "solid-js";
import { Button } from "~/components/ui/button";
import { Badge } from "~/components/ui/badge";
import { notify } from "~/components/ui/toast";
import {
  ApiError,
  previewAudition,
  previewSample,
  previewStoryboard,
  resolveApiUrl,
  ttsPreview,
} from "~/lib/api/client";
import type { PreviewStoryboardResult, VideoParams } from "~/lib/api/types";

export interface PreviewPanelProps {
  /** 人物/主题名（必填才允许预览）。 */
  person: () => string;
  /** 已有结构化分镜（可为空——空时后端按脚本/关键词确定性派生预览镜）。 */
  shots: () => Record<string, unknown>[];
  /** 脚本正文（粘贴/已有/AI 生成结果），用于派生预览镜与试听兜底旁白。 */
  script: () => string;
  /** 当前完整参数快照，样片档复用。 */
  params: () => VideoParams;
}

export function PreviewPanel(props: PreviewPanelProps) {
  const [tab, setTab] = createSignal<"storyboard" | "audition" | "sample">("storyboard");
  const [busy, setBusy] = createSignal(false);
  const [story, setStory] = createSignal<PreviewStoryboardResult | null>(null);
  // 记录上一次成功预览时的参数签名，签名变化即视为「已过期」。
  const [ranSignature, setRanSignature] = createSignal("");
  const [audioUrl, setAudioUrl] = createSignal("");
  const [sampleTaskId, setSampleTaskId] = createSignal("");

  const person = createMemo(() => (props.person() || "").trim());
  const shots = createMemo(() => props.shots() ?? []);

  // 参数签名：人物 + 逐镜检索词 + 脚本前缀 + 音色语速。任一变动 → 旧预览过期。
  const signature = createMemo(() =>
    JSON.stringify({
      person: person(),
      shots: shots().map((s) => `${s.visual_query ?? ""}|${s.visual_type ?? ""}`),
      script: (props.script() || "").slice(0, 400),
      voice: `${props.params().voice_name}|${props.params().voice_rate}`,
    }),
  );
  const stale = () => story() !== null && ranSignature() !== signature();

  const runStoryboard = async () => {
    if (!person()) {
      notify.error("请先填写人物或主题");
      return;
    }
    setBusy(true);
    try {
      // 无结构化分镜时传空 shots + video_script，后端确定性派生预览镜（不依赖 LLM）。
      const res = await previewStoryboard({
        person: person(),
        shots: shots(),
        video_script: props.script() || "",
      });
      setStory(res);
      setRanSignature(signature());
    } catch (e) {
      notify.error("分镜预览失败", e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const runAudition = async () => {
    const p = props.params();
    if (!p.voice_name) {
      notify.error("请先选择配音音色");
      return;
    }
    const narration =
      shots()
        .map((s) => String(s.narration ?? ""))
        .filter(Boolean)
        .join("") ||
      (props.script() || "").trim() ||
      person();
    if (!narration) {
      notify.info("没有可试听的旁白");
      return;
    }
    setBusy(true);
    try {
      const cut = await previewAudition({
        narration,
        voice_rate: p.voice_rate,
        max_seconds: 15,
      });
      const r = await ttsPreview({
        text: cut.text,
        voice_name: p.voice_name,
        voice_rate: p.voice_rate,
        voice_volume: p.voice_volume,
      });
      setAudioUrl(r.audio_url);
      new Audio(resolveApiUrl(r.audio_url)).play().catch(() => {
        notify.info("试听音频已生成，如未自动播放请检查浏览器权限");
      });
    } catch (e) {
      notify.error("试听失败", e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const runSample = async () => {
    setBusy(true);
    try {
      const res = await previewSample({ params: { ...props.params() }, max_seconds: 15 });
      setSampleTaskId(res.task_id);
      notify.success("样片任务已提交，可在任务页查看进度", res.task_id);
    } catch (e) {
      notify.error("样片提交失败", e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const TABS = [
    { value: "storyboard", label: "分镜" },
    { value: "audition", label: "试听" },
    { value: "sample", label: "样片" },
  ] as const;

  return (
    <div class="flex flex-col rounded-card border border-line bg-surface">
      <div class="flex gap-1 border-b border-line px-3 pt-2">
        <For each={TABS}>
          {(t) => (
            <button
              class={
                "-mb-px rounded-t-control px-3 py-1.5 text-sm font-semibold transition-colors duration-120 " +
                (tab() === t.value
                  ? "border-b-2 border-primary text-ink"
                  : "text-ink-dim hover:text-ink")
              }
              onClick={() => setTab(t.value)}
            >
              {t.label}
            </button>
          )}
        </For>
      </div>

      <div class="flex flex-col gap-3 p-3">
        <Show when={tab() === "storyboard"}>
          <div class="flex items-center justify-between">
            <Button variant="secondary" size="sm" disabled={busy()} onClick={() => void runStoryboard()}>
              {busy() ? "生成中…" : stale() ? "重新预览" : "预览分镜"}
            </Button>
            <Show when={stale()}>
              <Badge tone="warn">已过期</Badge>
            </Show>
          </div>
          <Show when={story()}>
            {(s) => (
              <div class="flex flex-col gap-2">
                <Show when={s().auto_derived}>
                  <p class="text-[11px] text-ink-dim">
                    尚无结构化分镜，已按脚本/关键词确定性派生预览镜（不依赖模型）。
                  </p>
                </Show>
                <div class="flex flex-wrap items-center gap-2 text-xs text-ink-dim">
                  {/* 三数指标：相关 N / 兜底 K / 缺失 L（与确认屏一致，兜底不算齐备）。 */}
                  <span class="font-semibold text-ok">相关 {s().relevant_shots}</span>
                  <span>·</span>
                  <span class={s().fallback_shots ? "font-semibold text-warn" : ""}>兜底 {s().fallback_shots}</span>
                  <span>·</span>
                  <span class={s().missing_shots_count ? "font-semibold text-err" : ""}>缺失 {s().missing_shots_count}</span>
                  <span>·</span>
                  <span>相关独立素材 {s().relevant_assets}</span>
                  <span>·</span>
                  <span>{s().shot_count} 镜</span>
                  <Show
                    when={s().fallback_shots === 0 && s().missing_shots_count === 0}
                    fallback={
                      <Badge tone={s().missing_shots_count ? "err" : "warn"}>
                        {s().missing_shots_count ? `缺画面 ${s().missing_shots_count}` : `含兜底 ${s().fallback_shots}`}
                      </Badge>
                    }
                  >
                    <Badge tone="ok">素材齐备</Badge>
                  </Show>
                </div>
                <div class="flex max-h-[42vh] flex-col gap-2 overflow-y-auto">
                  <For each={s().shots}>
                    {(shot) => (
                      <div class="rounded-card border border-line bg-surface-2 p-2">
                        <p class="line-clamp-2 text-xs text-ink">{shot.narration || "（无旁白）"}</p>
                        <p class="truncate text-[11px] text-ink-dim">
                          检索：{shot.query || "—"}
                          <Show when={shot.fallback_reason}>
                            <span class="ml-1 text-warn">· {shot.fallback_reason}</span>
                          </Show>
                        </p>
                        <Show when={shot.warning}>
                          <p class="mt-1 rounded-control bg-err/15 px-2 py-1 text-[11px] font-semibold text-err">
                            ⚠ {shot.warning}
                          </p>
                        </Show>
                        <div class="mt-1 flex flex-wrap gap-1">
                          <For each={shot.candidates.slice(0, 4)}>
                            {(c) => (
                              <a
                                href={c.source_page || c.thumbnail}
                                target="_blank"
                                rel="noreferrer"
                                title={`${c.title} · ${c.license || "未知授权"} · 相关性 ${c.relevance}`}
                                class={
                                  "overflow-hidden rounded-control border " +
                                  (c.passes_gate && !c.excluded ? "border-ok/40" : "border-line opacity-60")
                                }
                              >
                                <img src={c.thumbnail} alt={c.title} class="h-12 w-12 object-cover" loading="lazy" />
                              </a>
                            )}
                          </For>
                          <Show when={shot.candidates.length === 0}>
                            <span class="text-[11px] text-ink-dim">无候选</span>
                          </Show>
                        </div>
                      </div>
                    )}
                  </For>
                </div>
              </div>
            )}
          </Show>
        </Show>

        <Show when={tab() === "audition"}>
          <Button variant="secondary" size="sm" disabled={busy()} onClick={() => void runAudition()}>
            {busy() ? "合成中…" : "试听前 15 秒"}
          </Button>
          <Show when={audioUrl()}>
            <audio controls class="w-full" src={resolveApiUrl(audioUrl())} />
          </Show>
          <p class="text-[11px] text-ink-dim">按当前音色与语速合成开头约 15 秒。</p>
        </Show>

        <Show when={tab() === "sample"}>
          <Button variant="secondary" size="sm" disabled={busy()} onClick={() => void runSample()}>
            {busy() ? "提交中…" : "生成样片（约 15 秒）"}
          </Button>
          <Show when={sampleTaskId()}>
            <p class="text-[11px] text-ink-dim">
              样片任务 <code>{sampleTaskId()}</code> 已提交，请在任务页查看渲染进度。
            </p>
          </Show>
          <p class="text-[11px] text-ink-dim">走独立快路径提交短任务，套用当前字幕样式。</p>
        </Show>
      </div>
    </div>
  );
}
