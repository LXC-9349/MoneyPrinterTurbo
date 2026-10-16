// 向导·效果预览三档（里程碑 4 #4），位于摘要轨下方。
//  ① 分镜预览：逐镜卡片（旁白/候选缩略图/相关性/来源授权），顶部覆盖率与独立素材数，
//     缺素材/低相关醒目警告；秒级返回、不渲染。
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
import type { PreviewStoryboardResult } from "~/lib/api/types";
import { useWorkspace } from "~/lib/stores/workspace";

// 从草稿里取逐镜 timeline（传记/普通结构化脚本共用同一形状）。
function shotsFromDraft(sb: Record<string, unknown> | null): Record<string, unknown>[] {
  const timeline = (sb?.timeline as unknown[] | undefined) ?? [];
  return Array.isArray(timeline) ? (timeline as Record<string, unknown>[]) : [];
}

export function PreviewPanel() {
  const ws = useWorkspace();
  const [tab, setTab] = createSignal<"storyboard" | "audition" | "sample">("storyboard");
  const [busy, setBusy] = createSignal(false);
  const [story, setStory] = createSignal<PreviewStoryboardResult | null>(null);
  // 记录上一次成功预览时的参数签名，签名变化即视为「已过期」。
  const [ranSignature, setRanSignature] = createSignal("");
  const [audioUrl, setAudioUrl] = createSignal("");
  const [sampleTaskId, setSampleTaskId] = createSignal("");

  const person = () =>
    (ws.params.biography_person || ws.params.video_subject || "").trim();
  const shots = () => shotsFromDraft(ws.params.biography_script as Record<string, unknown> | null);

  // 参数签名：人物 + 逐镜检索词 + 相关性档位 + 音色语速。任一变动 → 旧预览过期。
  const signature = createMemo(() =>
    JSON.stringify({
      person: person(),
      shots: shots().map((s) => `${s.visual_query ?? ""}|${s.visual_type ?? ""}`),
      voice: `${ws.params.voice_name}|${ws.params.voice_rate}`,
    }),
  );
  const stale = () => story() !== null && ranSignature() !== signature();

  const runStoryboard = async () => {
    if (!person()) {
      notify.error("请先在内容步填写人物或主题");
      return;
    }
    if (shots().length === 0) {
      notify.info("当前脚本没有结构化分镜，先生成脚本后再预览");
      return;
    }
    setBusy(true);
    try {
      const res = await previewStoryboard({ person: person(), shots: shots() });
      setStory(res);
      setRanSignature(signature());
    } catch (e) {
      notify.error("分镜预览失败", e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const runAudition = async () => {
    if (!ws.params.voice_name) {
      notify.error("请先在声音步选择音色");
      return;
    }
    const narration =
      shots()
        .map((s) => String(s.narration ?? ""))
        .filter(Boolean)
        .join("") ||
      (ws.params.video_script || "").trim() ||
      person();
    if (!narration) {
      notify.info("没有可试听的旁白");
      return;
    }
    setBusy(true);
    try {
      const cut = await previewAudition({
        narration,
        voice_rate: ws.params.voice_rate,
        max_seconds: 15,
      });
      const r = await ttsPreview({
        text: cut.text,
        voice_name: ws.params.voice_name,
        voice_rate: ws.params.voice_rate,
        voice_volume: ws.params.voice_volume,
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
      const res = await previewSample({ params: { ...ws.params }, max_seconds: 15 });
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
    <Card2>
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
                <div class="flex items-center gap-2 text-xs text-ink-dim">
                  <span>覆盖率 {Math.round(s().coverage * 100)}%</span>
                  <span>·</span>
                  <span>独立素材 {s().independent_assets}</span>
                  <span>·</span>
                  <span>{s().shot_count} 镜</span>
                  <Badge tone={s().missing_shots.length ? "err" : "ok"}>
                    {s().missing_shots.length ? `缺素材 ${s().missing_shots.length}` : "素材齐备"}
                  </Badge>
                </div>
                <div class="flex max-h-72 flex-col gap-2 overflow-y-auto">
                  <For each={s().shots}>
                    {(shot) => (
                      <div class="rounded-card border border-line bg-surface-2 p-2">
                        <p class="line-clamp-2 text-xs text-ink">{shot.narration || "（无旁白）"}</p>
                        <p class="truncate text-[11px] text-ink-dim">检索：{shot.query || "—"}</p>
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
    </Card2>
  );
}

// 复用 Card 视觉但不带标题栏（右侧窄轨内嵌）。
function Card2(props: { children?: any }) {
  return <div class="rounded-card border border-line bg-surface">{props.children}</div>;
}
