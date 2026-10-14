// 向导·内容步：传记模式提供人物选择 + 基础史料勾选；普通模式提供主题/语言/
// 段落数/文案，并可调用 LLM 生成文案与关键词。字段全部双向绑定到 workspace 的
// draft store（细粒度更新），前进校验门在 store.canProceed("content")。
import { createSignal, For, Show } from "solid-js";
import { createAsync } from "@solidjs/router";
import { TextField } from "~/components/ui/text-field";
import { TextArea } from "~/components/ui/text-area";
import { Select } from "~/components/ui/select";
import { Button } from "~/components/ui/button";
import { notify } from "~/components/ui/toast";
import { languagesQuery, personDocsQuery, personsQuery } from "~/lib/api/data";
import { generateScript, generateTerms, ApiError } from "~/lib/api/client";
import { useWorkspace } from "~/lib/stores/workspace";

export function ContentStep(props: { projectId: string }) {
  const ws = useWorkspace();
  const [keyword, setKeyword] = createSignal("");
  const [busy, setBusy] = createSignal(false);

  const languages = createAsync(() => languagesQuery());

  // 传记：人物列表按关键词服务端筛选（去抖交给用户的搜索按钮/输入停顿）。
  const persons = createAsync(() =>
    ws.mode() === "biography" ? personsQuery({ keyword: keyword() }) : Promise.resolve(null),
  );
  // 选中人物后拉取其史料文档，供勾选基础脚本（biography_doc_ids）。
  const docs = createAsync(() => {
    const p = ws.params.biography_person;
    return p ? personDocsQuery(p) : Promise.resolve(null);
  });

  const runGenerateScript = async () => {
    setBusy(true);
    try {
      const subject =
        ws.mode() === "biography" ? ws.params.biography_person : ws.params.video_subject;
      const r = await generateScript({
        video_subject: subject,
        video_language: ws.params.video_language,
        paragraph_number: ws.params.paragraph_number,
        video_script_prompt: ws.params.video_script_prompt,
        custom_system_prompt: ws.params.custom_system_prompt,
      });
      if (r.video_script) {
        ws.setField("video_script", r.video_script);
        notify.success("文案已生成");
      } else {
        notify.error("文案生成失败", "请检查 LLM 配置或稍后重试");
      }
    } catch (e) {
      notify.error("文案生成失败", e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const runGenerateTerms = async () => {
    setBusy(true);
    try {
      const r = await generateTerms({
        video_subject: ws.params.video_subject,
        video_script: ws.params.video_script,
        amount: 5,
        match_materials_to_script: ws.params.match_materials_to_script,
      });
      ws.setField("video_terms", r.video_terms);
      notify.success(`已生成 ${r.video_terms.length} 个关键词`);
    } catch (e) {
      notify.error("关键词生成失败", e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const toggleDoc = (docId: string, checked: boolean) => {
    const current = new Set(ws.params.biography_doc_ids);
    if (checked) current.add(docId);
    else current.delete(docId);
    ws.setField("biography_doc_ids", [...current]);
  };

  return (
    <div class="flex flex-col gap-4">
      <Show when={ws.mode() === "biography"}>
        <div class="flex flex-col gap-2">
          <div class="flex items-end gap-2">
            <TextField
              class="flex-1"
              label="搜索人物"
              value={keyword()}
              onInput={setKeyword}
              placeholder="按姓名关键词筛选史料库"
            />
          </div>
          <Select
            label="传记人物"
            placeholder="选择一位人物"
            value={ws.params.biography_person || undefined}
            options={(persons()?.persons ?? []).map((p) => ({
              value: p.person,
              label: p.dynasty ? `${p.person}（${p.dynasty}）` : p.person,
            }))}
            onChange={(v) => {
              ws.setField("biography_person", v ?? "");
              // 换人物时重置基础脚本勾选。
              if (v !== ws.params.biography_person) ws.setField("biography_doc_ids", []);
            }}
          />
          <Show when={docs()}>
            {(d) => (
              <div class="rounded-card border border-line bg-surface-2 p-3">
                <p class="mb-2 text-sm font-semibold text-ink">
                  基础史料（勾选作为文案事实来源）
                </p>
                <Show
                  when={d().documents.length > 0}
                  fallback={<p class="text-xs text-ink-dim">该人物暂无可勾选史料。</p>}
                >
                  <ul class="flex max-h-52 flex-col gap-1 overflow-y-auto">
                    <For each={d().documents}>
                      {(doc) => (
                        <li class="flex items-center gap-2 text-sm text-ink">
                          <input
                            type="checkbox"
                            checked={ws.params.biography_doc_ids.includes(doc.doc_id)}
                            onChange={(e) => toggleDoc(doc.doc_id, e.currentTarget.checked)}
                          />
                          <span class="flex-1 truncate">{doc.title || doc.doc_id}</span>
                          <span class="text-xs text-ink-dim">{doc.category}</span>
                        </li>
                      )}
                    </For>
                  </ul>
                </Show>
              </div>
            )}
          </Show>
        </div>
      </Show>

      <Show when={ws.mode() !== "biography"}>
        <TextField
          label="视频主题"
          required
          value={ws.params.video_subject}
          onInput={(v) => ws.setField("video_subject", v)}
          placeholder="例如：春天的花海"
        />
      </Show>

      <div class="grid grid-cols-2 gap-3">
        <Select
          label="语言"
          placeholder="自动检测"
          value={ws.params.video_language || undefined}
          options={(languages()?.languages ?? []).map((l) => ({ value: l.value, label: l.label }))}
          onChange={(v) => ws.setField("video_language", v ?? "")}
        />
        <TextField
          label="段落数"
          type="number"
          value={String(ws.params.paragraph_number)}
          onInput={(v) => ws.setField("paragraph_number", Math.max(1, Number(v) || 1))}
        />
      </div>

      <TextArea
        id="video-script"
        label="文案"
        rows={8}
        value={ws.params.video_script}
        onInput={(v) => ws.setField("video_script", v)}
        placeholder="留空可点击下方按钮由 LLM 生成，或直接粘贴你的文案"
      />

      <Show when={ws.mode() !== "biography"}>
        <div class="flex flex-wrap gap-2">
          <Button variant="secondary" size="sm" disabled={busy()} onClick={runGenerateScript}>
            生成文案
          </Button>
          <Button variant="ghost" size="sm" disabled={busy()} onClick={runGenerateTerms}>
            生成关键词
          </Button>
          <Show when={(ws.params.video_terms ?? []).toString() !== ""}>
            <span class="self-center text-xs text-ink-dim">
              关键词：{Array.isArray(ws.params.video_terms)
                ? ws.params.video_terms.join("、")
                : ws.params.video_terms}
            </span>
          </Show>
        </div>
      </Show>

      <Show when={ws.mode() === "biography"}>
        <div class="flex flex-wrap gap-2">
          <Button variant="secondary" size="sm" disabled={busy()} onClick={runGenerateScript}>
            基于史料生成文案
          </Button>
        </div>
      </Show>
    </div>
  );
}
