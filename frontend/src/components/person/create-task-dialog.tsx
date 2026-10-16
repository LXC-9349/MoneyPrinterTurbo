// 里程碑 2 · 创建任务弹窗（先不含效果预览，预览在里程碑 4）。
// 参数分组（内容/视觉/声音/字幕）+ 预设 + 脚本来源三选（AI 生成 / 使用已有脚本版本 /
// 粘贴脚本）+ 存为草稿 + 创建并开始。
// 脚本来源为「已有 / 粘贴」时写入 video_script —— 流水线 generate_script 见非空即
// 跳过 LLM 重新生成（#4）。创建走与向导完全一致的记录路径：
//   createProject(biography) → createVideoTask(POST /videos，record_task 落 tasks 表)
//   → patchProject(bind_task_id) —— 人物卡片统计与进行中抽屉因此同步反映。
import { For, Show, createMemo, createSignal, onMount } from "solid-js";
import { createStore } from "solid-js/store";
import { bgmsQuery, llmStatusQuery, videoSourcesQuery, voicesQuery } from "~/lib/api/data";
import { createProject, createVideoTask, patchProject, saveDraft } from "~/lib/api/client";
import type {
  BgmListResult,
  LlmStatusResult,
  VideoParams,
  VideoSourcesResult,
  VoicesResult,
} from "~/lib/api/types";
import { defaultVideoParams, PRESETS, type Preset } from "~/lib/defaults";
import { cn } from "~/lib/cn";
import { DialogRoot } from "~/components/ui/dialog";
import { Button } from "~/components/ui/button";
import { Select } from "~/components/ui/select";
import { KSlider } from "~/components/ui/slider";
import { KSwitch } from "~/components/ui/switch";
import { TextArea } from "~/components/ui/text-area";
import { TextField } from "~/components/ui/text-field";
import { notify } from "~/components/ui/toast";
import { PreviewPanel } from "./preview-panel";

export interface ExistingScript {
  task_id: string;
  label: string;
  script: string;
}

type ScriptSource = "ai" | "existing" | "paste";

// 分阶段确认（里程碑 4 #5）：门槛偏好本地持久化，首次默认开启两档。
const GATES_KEY = "mpt.pause-gates";
const ALL_GATES = ["script", "materials"] as const;
function readStoredGates(): string[] {
  try {
    const raw = localStorage.getItem(GATES_KEY);
    // 从未设置过：首次默认开启（脚本后 + 素材后）。
    if (raw === null) return [...ALL_GATES];
    const arr = JSON.parse(raw) as unknown;
    return Array.isArray(arr)
      ? ALL_GATES.filter((g) => (arr as unknown[]).includes(g))
      : [];
  } catch {
    return [...ALL_GATES];
  }
}

const ASPECT_OPTIONS = [
  { value: "9:16", label: "竖屏 9:16" },
  { value: "16:9", label: "横屏 16:9" },
  { value: "1:1", label: "方形 1:1" },
];

function Section(props: { title: string; hint?: string; children: any }) {
  return (
    <section class="flex flex-col gap-3 rounded-card border border-line bg-surface-2/50 p-3">
      <div>
        <h4 class="text-xs font-bold uppercase tracking-wide text-ink-dim">{props.title}</h4>
        <Show when={props.hint}><p class="mt-0.5 text-[0.7rem] text-ink-dim">{props.hint}</p></Show>
      </div>
      {props.children}
    </section>
  );
}

export function CreateTaskDialog(props: {
  person: string;
  open: boolean;
  onClose: () => void;
  existingScripts?: ExistingScript[];
  onCreated?: (taskId: string) => void;
}) {
  const [params, setParams] = createStore<VideoParams>(defaultVideoParams(props.person));
  const [title, setTitle] = createSignal("");
  const [source, setSource] = createSignal<ScriptSource>("ai");
  const [busy, setBusy] = createSignal(false);

  // 分阶段确认开关：读取本地偏好（首次默认开），写入 params.pause_gates。
  const hasGate = (g: string) => (params.pause_gates ?? []).includes(g);
  const toggleGate = (g: string, on: boolean) => {
    const cur = new Set(params.pause_gates ?? []);
    if (on) cur.add(g);
    else cur.delete(g);
    const next = ALL_GATES.filter((x) => cur.has(x));
    setParams("pause_gates", next);
    try {
      localStorage.setItem(GATES_KEY, JSON.stringify(next));
    } catch {
      // localStorage 不可用时仅本次会话生效，不影响创建。
    }
  };

  // 下拉数据在客户端挂载后拉取（弹窗仅打开时挂载），避免 createAsync 触发页面级 Suspense 闪白。
  const [voices, setVoices] = createSignal<VoicesResult>();
  const [bgms, setBgms] = createSignal<BgmListResult>();
  const [sources, setSources] = createSignal<VideoSourcesResult>();
  // 批次 C：LLM 就绪状态（纯配置判定）。未取到时不报警，取到且 ready=false 时醒目提示。
  const [llmStatus, setLlmStatus] = createSignal<LlmStatusResult>();
  const llmReady = createMemo(() => llmStatus()?.llm?.ready ?? true);
  const llmProvider = createMemo(() => llmStatus()?.llm?.provider ?? "");
  const llmFallback = createMemo(() => llmStatus()?.llm?.fallback ?? "");
  onMount(() => {
    setParams("pause_gates", readStoredGates());
    voicesQuery("").then(setVoices).catch(() => {});
    bgmsQuery().then(setBgms).catch(() => {});
    videoSourcesQuery().then(setSources).catch(() => {});
    llmStatusQuery().then(setLlmStatus).catch(() => {});
  });

  const scriptOptions = createMemo(() =>
    (props.existingScripts ?? []).map((s) => ({ value: s.task_id, label: s.label })),
  );

  const applyPreset = (p: Preset) => setParams(p.patch);

  const onExistingPick = (taskId: string | null) => {
    const hit = (props.existingScripts ?? []).find((s) => s.task_id === taskId);
    if (hit) setParams("video_script", hit.script);
  };

  const buildSnapshot = (): VideoParams => {
    const snap: VideoParams = { ...params };
    // 脚本来源守卫：粘贴/已有时确保 video_script 生效（跳过 LLM）；AI 生成就清空。
    if (source() === "ai") snap.video_script = "";
    snap.biography_person = props.person;
    if (!snap.video_subject?.trim()) snap.video_subject = props.person;
    return snap;
  };

  const validate = (): string => {
    if (source() === "paste" && !params.video_script.trim()) return "粘贴脚本模式下请填写脚本内容。";
    if (source() === "existing" && !params.video_script.trim()) return "请选择一个已有脚本版本。";
    return "";
  };

  const onSaveDraft = async () => {
    setBusy(true);
    try {
      const row = await saveDraft({
        person: props.person,
        title: title().trim() || props.person,
        params: buildSnapshot() as unknown as Record<string, unknown>,
      });
      notify.success("已存为草稿", row.task_id);
    } catch (e) {
      notify.error("存草稿失败", e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const onCreate = async () => {
    const err = validate();
    if (err) {
      notify.info("还差一步", err);
      return;
    }
    setBusy(true);
    try {
      const snap = buildSnapshot();
      // 与向导 submitGenerate 同一路径：先建项目条目，再 POST /videos 生成并绑定。
      const entry = await createProject("biography", title().trim() || props.person);
      const created = await createVideoTask(snap);
      await patchProject(entry.id, {
        draft: snap,
        step: "output",
        status: "processing",
        bind_task_id: created.task_id,
      });
      notify.success("任务已创建并开始", `${props.person} · ${created.task_id}`);
      props.onCreated?.(created.task_id);
      props.onClose();
    } catch (e) {
      notify.error("创建失败", e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <DialogRoot
      open={props.open}
      onOpenChange={props.onClose}
      title={`为「${props.person}」创建生成任务`}
      contentClass="!max-w-6xl"
    >
      <div class="grid max-h-[72vh] grid-cols-1 gap-4 overflow-hidden lg:grid-cols-[minmax(0,1fr)_360px]">
      <div class="flex min-h-0 flex-col gap-4 overflow-y-auto pr-1">
        {/* 批次 C：LLM 就绪状态灯 + 不可达醒目警告 */}
        <div class="flex items-center gap-2">
          <span
            class={cn(
              "inline-block h-2.5 w-2.5 rounded-full",
              llmReady() ? "bg-emerald-500" : "bg-red-500",
            )}
            aria-hidden
          />
          <span class="text-xs text-ink-dim">
            模型状态：{llmReady() ? "就绪" : "不可达"}
            <Show when={llmProvider()}>
              <span class="ml-1 opacity-70">（{llmProvider()}）</span>
            </Show>
          </span>
        </div>
        <Show when={!llmReady()}>
          <div class="rounded-card border border-red-500/40 bg-red-500/10 p-3">
            <p class="text-sm font-bold text-red-500">LLM 当前不可达（缺少必需配置）</p>
            <p class="mt-1 text-xs text-ink">
              “AI 生成”脚本与检索词会即时失败并回退到
              <Show when={llmFallback()} fallback="确定性兜底检索词">
                备用模型（{llmFallback()}）或确定性兜底检索词
              </Show>
              ，画面/文案质量可能下降。可改用“已有脚本”或“粘贴脚本”跳过 AI 生成，或到设置页配置模型 Key。
            </p>
          </div>
        </Show>

        {/* 预设 */}
        <div class="flex flex-wrap items-center gap-2">
          <span class="text-xs font-medium text-ink-dim">预设：</span>
          <For each={PRESETS}>
            {(p) => (
              <button
                type="button"
                title={p.hint}
                class={cn(
                  "rounded-full border px-3 py-1 text-xs font-medium transition-colors",
                  "border-line bg-surface-2 text-ink hover:border-line-strong",
                )}
                onClick={() => applyPreset(p)}
              >
                {p.label}
              </button>
            )}
          </For>
          <div class="ml-auto w-56">
            <TextField label="作品标题" value={title()} onInput={setTitle} placeholder={props.person} />
          </div>
        </div>

        {/* 内容 / 脚本来源 */}
        <Section title="内容与脚本" hint="已有/粘贴脚本会跳过 AI 重新生成，直接用该文案配音。">
          <div class="flex gap-1">
            {([
              ["ai", "AI 生成"],
              ["existing", "使用已有脚本版本"],
              ["paste", "粘贴脚本"],
            ] as [ScriptSource, string][]).map(([v, label]) => (
              <button
                type="button"
                class={cn(
                  "rounded-control px-3 py-1 text-xs font-semibold transition-colors",
                  source() === v ? "bg-primary text-white" : "bg-surface-2 text-ink-dim hover:text-ink",
                )}
                onClick={() => {
                  setSource(v);
                  if (v === "ai") setParams("video_script", "");
                }}
              >
                {label}
              </button>
            ))}
          </div>

          <Show when={source() === "ai"}>
            <TextArea
              id="script-prompt"
              label="生成要求（可选）"
              value={params.video_script_prompt}
              onInput={(v) => setParams("video_script_prompt", v)}
              rows={3}
              placeholder="例如：突出早年征战，语气沉郁，面向年轻观众"
            />
          </Show>

          <Show when={source() === "existing"}>
            <Show
              when={scriptOptions().length > 0}
              fallback={<p class="text-xs text-warn">该人物还没有可复用的历史脚本，请先用 AI 生成或粘贴。</p>}
            >
              <Select
                label="选择脚本版本"
                placeholder="选择一个历史任务的脚本"
                value={null}
                options={scriptOptions()}
                onChange={onExistingPick}
              />
            </Show>
          </Show>

          <Show when={source() === "paste"}>
            <TextArea
              id="script-paste"
              label="脚本正文"
              value={params.video_script}
              onInput={(v) => setParams("video_script", v)}
              rows={6}
              placeholder="粘贴完整旁白文案…"
            />
          </Show>

          <Show when={source() === "existing" && params.video_script}>
            <TextArea
              id="script-edit"
              label="可继续编辑所选脚本"
              value={params.video_script}
              onInput={(v) => setParams("video_script", v)}
              rows={5}
            />
          </Show>
        </Section>

        {/* 视觉 */}
        <Section title="画面与素材">
          <div class="grid grid-cols-2 gap-3">
            <Select
              label="画幅"
              value={params.video_aspect}
              options={ASPECT_OPTIONS}
              onChange={(v) => setParams("video_aspect", (v ?? "9:16") as VideoParams["video_aspect"])}
            />
            <Select
              label="素材源"
              placeholder="取材来源"
              value={params.video_source || undefined}
              options={[
                { value: "historical_archive", label: "历史档案（史料库）" },
                ...(sources()?.sources ?? []).map((s) => ({ value: s, label: s })),
              ]}
              onChange={(v) => setParams("video_source", v ?? "historical_archive")}
            />
            <KSlider
              label="片段时长"
              value={params.video_clip_duration}
              minValue={1}
              maxValue={15}
              step={1}
              formatValue={(n) => `${n}s`}
              onChange={(v) => setParams("video_clip_duration", v)}
            />
            <KSlider
              label="生成条数"
              value={params.video_count}
              minValue={1}
              maxValue={5}
              step={1}
              onChange={(v) => setParams("video_count", v)}
            />
          </div>
          <KSwitch
            label="按文案匹配素材"
            checked={params.match_materials_to_script}
            onChange={(v) => setParams("match_materials_to_script", v)}
          />
        </Section>

        {/* 声音 */}
        <Section title="配音与音乐">
          <Select
            label="配音音色"
            placeholder="选择音色"
            value={params.voice_name || undefined}
            options={(voices()?.voices ?? []).map((v) => ({ value: v.value, label: v.label }))}
            onChange={(v) => setParams("voice_name", v ?? "")}
          />
          <div class="grid grid-cols-2 gap-3">
            <KSlider
              label="语速"
              value={params.voice_rate}
              minValue={0.5}
              maxValue={2}
              step={0.05}
              formatValue={(n) => `${n.toFixed(2)}x`}
              onChange={(v) => setParams("voice_rate", v)}
            />
            <KSlider
              label="音量"
              value={params.voice_volume}
              minValue={0}
              maxValue={2}
              step={0.05}
              formatValue={(n) => `${Math.round(n * 100)}%`}
              onChange={(v) => setParams("voice_volume", v)}
            />
          </div>
          <Select
            label="背景音乐"
            placeholder="不使用"
            value={params.bgm_file || undefined}
            options={(bgms()?.files ?? []).map((f) => ({ value: f.file, label: f.name }))}
            onChange={(v) => setParams("bgm_file", v ?? "")}
          />
          <KSlider
            label="背景音乐音量"
            value={params.bgm_volume}
            minValue={0}
            maxValue={1}
            step={0.05}
            formatValue={(n) => `${Math.round(n * 100)}%`}
            onChange={(v) => setParams("bgm_volume", v)}
          />
        </Section>

        {/* 字幕 */}
        <Section title="字幕">
          <KSwitch
            label="启用字幕"
            checked={params.subtitle_enabled}
            onChange={(v) => setParams("subtitle_enabled", v)}
          />
          <Show when={params.subtitle_enabled}>
            <div class="grid grid-cols-2 gap-3">
              <Select
                label="位置"
                value={params.subtitle_position}
                options={[
                  { value: "top", label: "顶部" },
                  { value: "bottom", label: "底部" },
                  { value: "center", label: "居中" },
                ]}
                onChange={(v) => setParams("subtitle_position", v ?? "bottom")}
              />
              <KSlider
                label="字号"
                value={params.font_size}
                minValue={20}
                maxValue={120}
                step={2}
                onChange={(v) => setParams("font_size", v)}
              />
            </div>
          </Show>
        </Section>

        {/* 高级：分阶段确认（里程碑 4 #5） */}
        <Section
          title="高级 · 分阶段确认"
          hint="开启后任务会在对应阶段暂停为“待确认”，你在任务详情确认/调整后继续。首次默认开启。"
        >
          <KSwitch
            label="脚本生成后暂停确认"
            checked={hasGate("script")}
            onChange={(v) => toggleGate("script", v)}
          />
          <KSwitch
            label="素材获取后暂停确认（可换图 / 排除 / 删镜）"
            checked={hasGate("materials")}
            onChange={(v) => toggleGate("materials", v)}
          />
          <Show
            when={hasGate("script") || hasGate("materials")}
            fallback={<p class="text-[0.7rem] text-ink-dim">两档均关闭：任务将一次性跑完，不暂停。</p>}
          >
            <p class="text-[0.7rem] text-ink-dim">
              已开启 {(params.pause_gates ?? []).length} 档，可在“任务”页“待我确认”计数处查看。
            </p>
          </Show>
        </Section>
      </div>

      {/* 批次 D：右侧效果预览三档（props 驱动；无结构化分镜时后端确定性派生预览镜） */}
      <aside class="hidden min-h-0 flex-col overflow-y-auto border-l border-line pl-3 lg:flex">
        <h4 class="mb-2 text-xs font-bold uppercase tracking-wide text-ink-dim">效果预览</h4>
        <PreviewPanel
          person={() => props.person}
          shots={() => []}
          script={() => (source() === "ai" ? "" : params.video_script)}
          params={() => buildSnapshot()}
        />
      </aside>
      </div>

      {/* 底部操作 */}
      <div class="mt-3 flex items-center justify-end gap-2 border-t border-line pt-3">
        <Button variant="ghost" size="sm" disabled={busy()} onClick={props.onClose}>
          取消
        </Button>
        <Button variant="secondary" size="sm" disabled={busy()} onClick={() => void onSaveDraft()}>
          存为草稿
        </Button>
        <Button variant="primary" size="sm" disabled={busy()} onClick={() => void onCreate()}>
          {busy() ? "处理中…" : "创建并开始"}
        </Button>
      </div>
    </DialogRoot>
  );
}
