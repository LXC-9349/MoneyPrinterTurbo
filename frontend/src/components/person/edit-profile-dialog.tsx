// 编辑资料弹窗（里程碑 2 收尾 #5）：别名、朝代、类别、备注、标签、分组。
// 沿用 CreateTaskDialog 的“仅打开时挂载”模式（父组件 <Show> 包裹），不参与 SSR。
// 保存组合三类请求：① patchPerson 写叠加层（dynasty/category/note/tags，
// personal.sqlite）；② setPersonGroups 整替分组；③ 别名与初始值求 diff 后逐条
// add/remove（mpt.sqlite person_aliases，后端幂等、主名不可删）。
import { createAsync } from "@solidjs/router";
import { For, Show, createSignal } from "solid-js";
import { groupsQuery, personsQuery } from "~/lib/api/data";
import {
  addPersonAlias,
  patchPerson,
  removePersonAlias,
  setPersonGroups,
} from "~/lib/api/client";
import type { PersonAliasItem, PersonProfile } from "~/lib/api/types";
import { Button } from "~/components/ui/button";
import { DialogRoot } from "~/components/ui/dialog";
import { notify } from "~/components/ui/toast";

// 别名来源徽标文案（person_aliases.source）。
const ALIAS_SOURCE_LABEL: Record<string, string> = {
  manual: "手动",
  import: "库导入",
  bio: "传记管线",
};

const inputCls =
  "w-full rounded-control border border-line bg-surface px-3 py-2 text-sm text-ink outline-none focus:border-primary";

export function EditProfileDialog(props: {
  person: string;
  profile: PersonProfile;
  aliases: PersonAliasItem[];
  onClose: () => void;
  onSaved: () => void;
}) {
  // 朝代/类别候选与分组选项：弹窗只在客户端打开时挂载，此时按需取数。
  const lib = createAsync(() => personsQuery({ include_hidden: true }));
  const groups = createAsync(() => groupsQuery());

  const [busy, setBusy] = createSignal(false);
  const [dynasty, setDynasty] = createSignal(props.profile.dynasty ?? "");
  const [category, setCategory] = createSignal(props.profile.category ?? "");
  const [note, setNote] = createSignal(props.profile.note ?? "");
  const [tagsText, setTagsText] = createSignal((props.profile.tags ?? []).join("，"));
  const [aliasList, setAliasList] = createSignal<string[]>(
    props.aliases.map((a) => a.alias),
  );
  const [aliasDraft, setAliasDraft] = createSignal("");
  const [picked, setPicked] = createSignal<string[]>(props.profile.groups ?? []);

  // 存量别名的来源标注（新增项无来源，保存时后端记 manual）。
  const sourceOf = new Map(props.aliases.map((a) => [a.alias, a.source]));

  // 逗号（中英文）/顿号分隔 → 去空白去重，上限与后端清洗一致（20 个）。
  const tagsParsed = () => {
    const parts = tagsText()
      .split(/[,，、]/)
      .map((t) => t.trim())
      .filter(Boolean);
    return Array.from(new Set(parts)).slice(0, 20);
  };

  const addAlias = () => {
    const v = aliasDraft().trim().slice(0, 40);
    if (!v || v === props.person) return;
    setAliasList((prev) => (prev.includes(v) ? prev : [...prev, v]));
    setAliasDraft("");
  };

  const toggleGroup = (g: string) =>
    setPicked((prev) =>
      prev.includes(g) ? prev.filter((x) => x !== g) : [...prev, g],
    );

  const save = async () => {
    if (busy()) return;
    setBusy(true);
    try {
      await patchPerson(props.person, {
        dynasty: dynasty().trim(),
        category: category().trim(),
        note: note().trim(),
        tags: tagsParsed(),
      });
      await setPersonGroups(props.person, picked());
      const initial = props.aliases.map((a) => a.alias);
      const current = aliasList();
      for (const a of current) {
        if (!initial.includes(a)) await addPersonAlias(props.person, a);
      }
      for (const a of initial) {
        if (!current.includes(a)) await removePersonAlias(props.person, a);
      }
      notify.success("资料已保存");
      props.onSaved();
      props.onClose();
    } catch (e) {
      notify.error("保存失败", e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <DialogRoot
      open
      onOpenChange={(v) => {
        if (!v) props.onClose();
      }}
      title={`编辑资料 · ${props.person}`}
      contentClass="max-w-xl"
    >
      <div class="flex flex-col gap-4">
        {/* 别名：chips 可删，输入框回车新增；diff 后保存 */}
        <div>
          <p class="mb-1.5 text-xs font-semibold text-ink-dim">别名</p>
          <div class="flex flex-wrap items-center gap-1.5">
            <For each={aliasList()}>
              {(a) => (
                <span class="flex items-center gap-1.5 rounded-control border border-line bg-surface-2 px-2 py-1 text-xs text-ink">
                  {a}
                  <Show when={sourceOf.get(a)}>
                    {(s) => (
                      <span class="text-ink-dim">{ALIAS_SOURCE_LABEL[s()] ?? s()}</span>
                    )}
                  </Show>
                  <button
                    type="button"
                    class="text-ink-dim hover:text-ink"
                    aria-label={`移除别名 ${a}`}
                    onClick={() => setAliasList((prev) => prev.filter((x) => x !== a))}
                  >
                    ×
                  </button>
                </span>
              )}
            </For>
            <input
              class="w-40 rounded-control border border-line bg-surface px-2 py-1 text-xs text-ink outline-none focus:border-primary"
              placeholder="新增别名（回车）"
              value={aliasDraft()}
              onInput={(e) => setAliasDraft(e.currentTarget.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") {
                  e.preventDefault();
                  addAlias();
                }
              }}
            />
          </div>
        </div>

        <div class="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <label class="block">
            <span class="mb-1.5 block text-xs font-semibold text-ink-dim">朝代</span>
            <input
              class={inputCls}
              list="edit-dynasty-options"
              placeholder="如：魏晋南北朝 / 当代 / 空"
              value={dynasty()}
              onInput={(e) => setDynasty(e.currentTarget.value)}
            />
            <datalist id="edit-dynasty-options">
              <For each={lib()?.dynasties ?? []}>{(d) => <option value={d} />}</For>
            </datalist>
          </label>
          <label class="block">
            <span class="mb-1.5 block text-xs font-semibold text-ink-dim">类别</span>
            <input
              class={inputCls}
              list="edit-category-options"
              placeholder="如：政治家 / 文学家"
              value={category()}
              onInput={(e) => setCategory(e.currentTarget.value)}
            />
            <datalist id="edit-category-options">
              <For each={lib()?.categories ?? []}>{(c) => <option value={c} />}</For>
            </datalist>
          </label>
        </div>

        <label class="block">
          <span class="mb-1.5 block text-xs font-semibold text-ink-dim">备注</span>
          <textarea
            class={`${inputCls} min-h-[4.5rem]`}
            placeholder="一句话人物备注（显示在卡片与详情页）"
            value={note()}
            onInput={(e) => setNote(e.currentTarget.value)}
          />
        </label>

        <label class="block">
          <span class="mb-1.5 block text-xs font-semibold text-ink-dim">
            标签（逗号分隔，最多 20 个）
          </span>
          <input
            class={inputCls}
            placeholder="如：枭雄，建安文学"
            value={tagsText()}
            onInput={(e) => setTagsText(e.currentTarget.value)}
          />
        </label>

        <div>
          <p class="mb-1.5 text-xs font-semibold text-ink-dim">分组</p>
          <Show
            when={(groups()?.groups ?? []).length > 0}
            fallback={<p class="text-xs text-ink-dim">还没有分组，可在工作台「分组管理」创建。</p>}
          >
            <div class="flex flex-wrap gap-1.5">
              <For each={groups()?.groups ?? []}>
                {(g) => (
                  <label
                    class={`flex cursor-pointer items-center gap-1.5 rounded-control border px-2 py-1 text-xs ${
                      picked().includes(g.name)
                        ? "border-primary bg-primary/10 text-ink"
                        : "border-line bg-surface text-ink-dim"
                    }`}
                  >
                    <input
                      type="checkbox"
                      class="accent-primary"
                      checked={picked().includes(g.name)}
                      onChange={() => toggleGroup(g.name)}
                    />
                    {g.name}
                    <span class="text-ink-dim">{g.count}</span>
                  </label>
                )}
              </For>
            </div>
          </Show>
        </div>

        <div class="flex justify-end gap-2 border-t border-line pt-3">
          <Button size="sm" variant="ghost" disabled={busy()} onClick={props.onClose}>
            取消
          </Button>
          <Button size="sm" variant="primary" disabled={busy()} onClick={() => void save()}>
            {busy() ? "保存中…" : "保存"}
          </Button>
        </div>
      </div>
    </DialogRoot>
  );
}
