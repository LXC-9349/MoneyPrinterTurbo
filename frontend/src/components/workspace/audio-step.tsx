// 向导·声音步：音色 / 语速 / 音量 / 背景音乐 / 试听。音色来自 /options/voices
// （按当前活动 provider），背景音乐来自 /musics。试听调用 POST /tts/preview 并用
// 原生 Audio 播放返回的相对 URL（媒体标签跨源加载不受 CORS 约束）。
import { Show, createSignal } from "solid-js";
import { createAsync } from "@solidjs/router";
import { Select } from "~/components/ui/select";
import { KSlider } from "~/components/ui/slider";
import { Button } from "~/components/ui/button";
import { notify } from "~/components/ui/toast";
import { bgmsQuery, voicesQuery } from "~/lib/api/data";
import { ApiError, resolveApiUrl, ttsPreview, uploadBgm } from "~/lib/api/client";
import { useWorkspace } from "~/lib/stores/workspace";

export function AudioStep() {
  const ws = useWorkspace();
  const voices = createAsync(() => voicesQuery(""));
  const bgms = createAsync(() => bgmsQuery());
  const [previewing, setPreviewing] = createSignal(false);
  const [uploading, setUploading] = createSignal(false);

  const preview = async () => {
    const text =
      (ws.params.video_script || "").trim() ||
      (ws.params.video_subject || "").trim() ||
      "这是一段试听文案。";
    if (!ws.params.voice_name) {
      notify.error("请先选择音色");
      return;
    }
    setPreviewing(true);
    try {
      const r = await ttsPreview({
        text,
        voice_name: ws.params.voice_name,
        voice_rate: ws.params.voice_rate,
        voice_volume: ws.params.voice_volume,
      });
      new Audio(resolveApiUrl(r.audio_url)).play().catch(() => {
        notify.info("试听音频已生成，如未自动播放请检查浏览器权限");
      });
    } catch (e) {
      notify.error("试听失败", e instanceof ApiError ? e.message : String(e));
    } finally {
      setPreviewing(false);
    }
  };

  const onUploadBgm = async (files: FileList | null) => {
    if (!files || files.length === 0) return;
    setUploading(true);
    try {
      const r = await uploadBgm(files[0]);
      ws.setField("bgm_file", r.file);
      notify.success("背景音乐已上传并选中");
    } catch (e) {
      notify.error("上传失败", e instanceof ApiError ? e.message : String(e));
    } finally {
      setUploading(false);
    }
  };

  return (
    <div class="flex flex-col gap-4">
      <Select
        label="配音音色"
        placeholder="选择音色"
        value={ws.params.voice_name || undefined}
        options={(voices()?.voices ?? []).map((v) => ({ value: v.value, label: v.label }))}
        onChange={(v) => ws.setField("voice_name", v ?? "")}
      />
      <Show when={voices()} fallback={<p class="text-xs text-ink-dim">正在加载音色…</p>}>
        <p class="text-xs text-ink-dim">当前配音引擎：{voices()?.provider}</p>
      </Show>

      <div class="grid grid-cols-2 gap-4">
        <KSlider
          label="语速"
          value={ws.params.voice_rate}
          minValue={0.5}
          maxValue={2}
          step={0.05}
          formatValue={(n) => `${n.toFixed(2)}x`}
          onChange={(v) => ws.setField("voice_rate", v)}
        />
        <KSlider
          label="音量"
          value={ws.params.voice_volume}
          minValue={0}
          maxValue={2}
          step={0.05}
          formatValue={(n) => `${Math.round(n * 100)}%`}
          onChange={(v) => ws.setField("voice_volume", v)}
        />
      </div>

      <Button variant="secondary" size="sm" disabled={previewing()} onClick={preview}>
        {previewing() ? "合成中…" : "试听当前音色"}
      </Button>

      <div class="my-1 h-px bg-line" />

      <Select
        label="背景音乐"
        placeholder="不使用"
        value={ws.params.bgm_file || undefined}
        options={[
          ...(bgms()?.files ?? []).map((f) => ({ value: f.file, label: f.name })),
        ]}
        onChange={(v) => ws.setField("bgm_file", v ?? "")}
      />
      <input
        type="file"
        accept="audio/*"
        disabled={uploading()}
        class="text-sm text-ink-dim file:mr-2 file:rounded-control file:border-0 file:bg-surface-2 file:px-3 file:py-1 file:text-ink"
        onChange={(e) => void onUploadBgm(e.currentTarget.files)}
      />
      <KSlider
        label="背景音乐音量"
        value={ws.params.bgm_volume}
        minValue={0}
        maxValue={1}
        step={0.05}
        formatValue={(n) => `${Math.round(n * 100)}%`}
        onChange={(v) => ws.setField("bgm_volume", v)}
      />
    </div>
  );
}
