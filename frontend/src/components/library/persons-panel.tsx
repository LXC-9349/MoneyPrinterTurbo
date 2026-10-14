// 人物管理面板：搜索/筛选、新建、编辑、置顶/归档、分组归属。
// 读走 readPersons/readGroups 服务端代理，写走 client.ts 直连。
// filters 以独立 signal 承载，派生 memo 作为 createEffect 依赖，源变化即重新拉取；
// 写操作后调用 reload() 保持列表与服务端一致。
import {
  For,
  Show,
  createEffect,
  createMemo,
  createSignal,
  on,
  onMount,
} from "solid-js";
import { Button } from "~/components/ui/button";
import { Card, CardContent } from "~/components/ui/card";
import { Badge } from "~/components/ui/badge";
import { TextField } from "~/components/ui/text-field";
import { TextArea } from "~/components/ui/text-area";
import { Select } from "~/components/ui/select";
import { KSwitch } from "~/components/ui/switch";
import { DialogRoot } from "~/components/ui/dialog";
import { notify } from "~/components/ui/toast";
import { readGroups, readPersons } from "~/server/queries";
import {
  ApiError,
  createGroup,
  createPerson,
  deleteGroup,
  deletePerson,
  fetchPersonProfile,
  patchPerson,
  restorePerson,
  setPersonGroups,
} from "~/lib/api/client";
import type { GroupsResult, Person, PersonsResult } from "~/lib/api/types";

function errMsg(e: unknown): string {
  return e instanceof ApiError ? e.message : String(e);
}

// 把多行文本按行拆成去空的事实列表。
function splitFacts(text: string): string[] {
  return text
    .split("\n")
    .map((s) => s.trim())
    .filter(Boolean);
}

export function PersonsPanel() {
  const [keyword, setKeyword] = createSignal("");
  const [dynasty, setDynasty] = createSignal<string | null>("");
  const [category, setCategory] = createSignal<string | null>("");
  const [showHidden, setShowHidden] = createSignal(false);

  const [persons, setPersons] = createSignal<PersonsResult | null>(null);
  const [groupsData, setGroupsData] = createSignal<GroupsResult | null>(null);

  const filters = createMemo(() => ({
    keyword: keyword().trim(),
    dynasty: dynasty() ?? "",
    category: category() ?? "",
    include_hidden: showHidden(),
  }));

  const loadPersons = async () => {
    try {
      setPersons(await readPersons(filters()));
    } catch (e) {
      notify.error("加载人物失败", errMsg(e));
    }
  };
  const loadGroups = async () => {
    try {
      setGroupsData(await readGroups());
    } catch (e) {
      notify.error("加载分组失败", errMsg(e));
    }
  };
  const reload = () => {
    void loadPersons();
    void loadGroups();
  };

  onMount(() => {
    void loadPersons();
    void loadGroups();
  });
  // 筛选条件变化时重新拉取人物列表（跳过首帧，避免与 onMount 重复请求）。
  createEffect(on(filters, () => void loadPersons(), { defer: true }));

  const dynastyOptions = createMemo(() => [
    { value: "", label: "全部朝代" },
    ...(persons()?.dynasties ?? []).map((d) => ({ value: d, label: d })),
  ]);
  const categoryOptions = createMemo(() => [
    { value: "", label: "全部类别" },
    ...(persons()?.categories ?? []).map((c) => ({ value: c, label: c })),
  ]);
  const groupNames = createMemo(() => (groupsData()?.groups ?? []).map((g) => g.name));

  // ── 新建区 ────────────────────────────────────────────────────────────
  const [newOpen, setNewOpen] = createSignal(false);
  const [nName, setNName] = createSignal("");
  const [nDynasty, setNDynasty] = createSignal("");
  const [nCategory, setNCategory] = createSignal("");
  const [nNote, setNNote] = createSignal("");
  const [nFacts, setNFacts] = createSignal("");
  const [nBusy, setNBusy] = createSignal(false);

  const submitNew = async () => {
    const name = nName().trim();
    if (!name) {
      notify.error("请填写人物姓名");
      return;
    }
    setNBusy(true);
    try {
      await createPerson({
        person: name,
        dynasty: nDynasty().trim(),
        category: nCategory().trim(),
        note: nNote().trim(),
        facts: splitFacts(nFacts()),
      });
      notify.success("已创建人物", name);
      setNName("");
      setNDynasty("");
      setNCategory("");
      setNNote("");
      setNFacts("");
      setNewOpen(false);
      reload();
    } catch (e) {
      notify.error("创建失败", errMsg(e));
    } finally {
      setNBusy(false);
    }
  };

  // ── 分组管理 ──────────────────────────────────────────────────────────
  const [gName, setGName] = createSignal("");
  const [gDel, setGDel] = createSignal<string | null>("");
  const [gBusy, setGBusy] = createSignal(false);

  const submitGroup = async () => {
    const name = gName().trim();
    if (!name) return;
    setGBusy(true);
    try {
      await createGroup(name);
      notify.success("已创建分组", name);
      setGName("");
      void loadGroups();
    } catch (e) {
      notify.error("创建分组失败", errMsg(e));
    } finally {
      setGBusy(false);
    }
  };

  const removeGroup = async () => {
    const name = gDel();
    if (!name) return;
    if (!window.confirm(`确认删除分组「${name}」？成员关系会一并解除。`)) return;
    try {
      await deleteGroup(name);
      notify.success("已删除分组");
      setGDel(null);
      reload();
    } catch (e) {
      notify.error("删除分组失败", errMsg(e));
    }
  };

  // ── 编辑弹窗 ──────────────────────────────────────────────────────────
  const [editOpen, setEditOpen] = createSignal(false);
  const [editName, setEditName] = createSignal("");
  const [eDynasty, setEDynasty] = createSignal("");
  const [eCategory, setECategory] = createSignal("");
  const [eNote, setENote] = createSignal("");
  const [eFacts, setEFacts] = createSignal("");
  const [ePinned, setEPinned] = createSignal(false);
  const [eBusy, setEBusy] = createSignal(false);

  const openEditor = async (p: Person) => {
    setEditName(p.person);
    setEDynasty(p.dynasty || "");
    setECategory(p.category || "");
    setENote(p.note || "");
    setEFacts("");
    setEPinned(p.pinned);
    setEditOpen(true);
    // facts 列表接口不带，按需拉取叠加层档案回填。
    try {
      const profile = await fetchPersonProfile(p.person);
      setEFacts((profile.key_facts ?? []).join("\n"));
      setEDynasty(profile.dynasty || p.dynasty || "");
      setECategory(profile.category || p.category || "");
      setENote(profile.note || p.note || "");
    } catch (e) {
      notify.error("读取档案失败", errMsg(e));
    }
  };

  const saveEditor = async () => {
    const name = editName();
    if (!name) return;
    setEBusy(true);
    try {
      await patchPerson(name, {
        dynasty: eDynasty().trim(),
        category: eCategory().trim(),
        note: eNote().trim(),
        facts: splitFacts(eFacts()),
        pinned: ePinned(),
      });
      notify.success("已保存人物", name);
      setEditOpen(false);
      reload();
    } catch (e) {
      notify.error("保存失败", errMsg(e));
    } finally {
      setEBusy(false);
    }
  };

  const togglePin = async (p: Person) => {
    try {
      await patchPerson(p.person, { pinned: !p.pinned });
      void loadPersons();
    } catch (e) {
      notify.error("操作失败", errMsg(e));
    }
  };

  const removePerson = async (p: Person) => {
    if (!window.confirm(`确认删除/归档「${p.person}」？文库人物将归档，自建人物将删除。`))
      return;
    try {
      const r = await deletePerson(p.person);
      notify.success(r.outcome === "deleted" ? "已删除人物" : "已归档人物", p.person);
      void loadPersons();
    } catch (e) {
      notify.error("删除失败", errMsg(e));
    }
  };

  const restore = async (p: Person) => {
    try {
      await restorePerson(p.person);
      notify.success("已恢复人物", p.person);
      void loadPersons();
    } catch (e) {
      notify.error("恢复失败", errMsg(e));
    }
  };

  const assignGroups = async (p: Person, gs: string[]) => {
    try {
      await setPersonGroups(p.person, gs);
      notify.success("已保存分组归属", p.person);
      void loadPersons();
    } catch (e) {
      notify.error("保存分组失败", errMsg(e));
    }
  };

  return (
    <div class="flex flex-col gap-4">
      {/* 新建人物 */}
      <Card>
        <CardContent class="py-3">
          <button
            type="button"
            class="text-sm font-semibold text-ink hover:text-primary"
            onClick={() => setNewOpen((v) => !v)}
          >
            ＋ 新建 / 叠加编辑人物 {newOpen() ? "▲" : "▼"}
          </button>
          <Show when={newOpen()}>
            <div class="mt-3 grid grid-cols-1 gap-3 md:grid-cols-2">
              <div class="flex flex-col gap-2">
                <TextField label="姓名" value={nName()} onInput={setNName} placeholder="必填" />
                <TextField label="朝代" value={nDynasty()} onInput={setNDynasty} />
                <TextField label="类别" value={nCategory()} onInput={setNCategory} />
              </div>
              <div class="flex flex-col gap-2">
                <TextArea id="new-note" label="备注" value={nNote()} onInput={setNNote} rows={2} />
                <TextArea
                  id="new-facts"
                  label="关键事实（每行一条）"
                  value={nFacts()}
                  onInput={setNFacts}
                  rows={2}
                />
              </div>
              <div class="md:col-span-2">
                <Button variant="primary" disabled={nBusy()} onClick={() => void submitNew()}>
                  {nBusy() ? "创建中…" : "创建人物"}
                </Button>
              </div>
            </div>
          </Show>
        </CardContent>
      </Card>

      {/* 筛选 */}
      <div class="flex flex-wrap items-end gap-3">
        <div class="min-w-40 flex-1">
          <TextField label="搜索姓名" value={keyword()} onInput={setKeyword} placeholder="输入关键词" />
        </div>
        <Select
          label="朝代"
          class="w-40"
          value={dynasty()}
          options={dynastyOptions()}
          onChange={setDynasty}
        />
        <Select
          label="类别"
          class="w-40"
          value={category()}
          options={categoryOptions()}
          onChange={setCategory}
        />
        <KSwitch checked={showHidden()} onChange={setShowHidden} label="显示已归档" class="mb-1" />
      </div>

      {/* 分组管理 */}
      <Card>
        <CardContent class="py-3">
          <p class="mb-2 text-sm font-bold text-ink">分组管理</p>
          <div class="flex flex-wrap items-end gap-3">
            <div class="min-w-40 flex-1">
              <TextField label="新建分组" value={gName()} onInput={setGName} placeholder="分组名" />
            </div>
            <Button
              variant="secondary"
              disabled={gBusy() || !gName().trim()}
              onClick={() => void submitGroup()}
            >
              创建分组
            </Button>
            <div class="min-w-40">
              <Select
                label="删除分组"
                value={gDel()}
                options={groupNames().map((g) => ({ value: g, label: g }))}
                placeholder="选择分组"
                onChange={setGDel}
              />
            </div>
            <Button variant="danger" disabled={!gDel()} onClick={() => void removeGroup()}>
              删除分组
            </Button>
          </div>
          <Show when={groupNames().length > 0}>
            <p class="mt-2 text-xs text-ink-dim">现有分组：{groupNames().join("、")}</p>
          </Show>
        </CardContent>
      </Card>

      {/* 人物列表 */}
      <Show
        when={persons()}
        fallback={<p class="text-sm text-ink-dim">正在加载人物…</p>}
      >
        {(data) => (
          <div class="flex flex-col gap-3">
            <p class="text-xs text-ink-dim">共 {data().total} 位人物</p>
            <For each={data().persons.slice(0, 200)}>
              {(p) => (
                <PersonCard
                  person={p}
                  allGroups={groupNames()}
                  onEdit={() => void openEditor(p)}
                  onPin={() => void togglePin(p)}
                  onDelete={() => void removePerson(p)}
                  onRestore={() => void restore(p)}
                  onAssignGroups={(gs) => void assignGroups(p, gs)}
                />
              )}
            </For>
            <Show when={data().total > 200}>
              <p class="text-xs text-ink-dim">仅显示前 200 位，请缩小筛选范围查看更多。</p>
            </Show>
          </div>
        )}
      </Show>

      {/* 编辑弹窗 */}
      <DialogRoot open={editOpen()} onOpenChange={setEditOpen} title={`编辑人物 · ${editName()}`}>
        <div class="flex flex-col gap-3">
          <TextField label="朝代" value={eDynasty()} onInput={setEDynasty} />
          <TextField label="类别" value={eCategory()} onInput={setECategory} />
          <TextArea id="edit-note" label="备注" value={eNote()} onInput={setENote} rows={3} />
          <TextArea
            id="edit-facts"
            label="关键事实（每行一条）"
            value={eFacts()}
            onInput={setEFacts}
            rows={4}
          />
          <KSwitch checked={ePinned()} onChange={setEPinned} label="置顶" />
          <div class="flex justify-end gap-2">
            <Button variant="ghost" onClick={() => setEditOpen(false)}>
              取消
            </Button>
            <Button variant="primary" disabled={eBusy()} onClick={() => void saveEditor()}>
              {eBusy() ? "保存中…" : "保存"}
            </Button>
          </div>
        </div>
      </DialogRoot>
    </div>
  );
}

function PersonCard(props: {
  person: Person;
  allGroups: string[];
  onEdit: () => void;
  onPin: () => void;
  onDelete: () => void;
  onRestore: () => void;
  onAssignGroups: (gs: string[]) => void;
}) {
  const [openGroups, setOpenGroups] = createSignal(false);
  const [selected, setSelected] = createSignal<string[]>(props.person.groups ?? []);

  const toggleGroup = (g: string) =>
    setSelected((cur) => (cur.includes(g) ? cur.filter((x) => x !== g) : [...cur, g]));

  return (
    <Card>
      <CardContent class="py-3">
        <div class="flex flex-wrap items-center justify-between gap-2">
          <div class="flex items-center gap-2">
            <span class="text-sm font-bold text-ink">{props.person.person}</span>
            <Badge tone={props.person.source === "custom" ? "primary" : "neutral"}>
              {props.person.source === "custom" ? "自建" : "文库"}
            </Badge>
            <Show when={props.person.pinned}>
              <Badge tone="warn">置顶</Badge>
            </Show>
            <Show when={props.person.hidden}>
              <Badge tone="err">已归档</Badge>
            </Show>
          </div>
          <div class="flex flex-wrap items-center gap-2 text-xs text-ink-dim">
            <span>{props.person.dynasty || "—"}</span>
            <span>{props.person.category || "—"}</span>
            <span>{props.person.doc_count} 篇</span>
          </div>
        </div>

        <div class="mt-2 flex flex-wrap gap-2">
          <Button size="sm" variant="secondary" onClick={props.onEdit}>
            编辑
          </Button>
          <Button size="sm" variant="ghost" onClick={props.onPin}>
            {props.person.pinned ? "取消置顶" : "置顶"}
          </Button>
          <Show
            when={props.person.hidden}
            fallback={
              <Button size="sm" variant="danger" onClick={props.onDelete}>
                删除/归档
              </Button>
            }
          >
            <Button size="sm" variant="secondary" onClick={props.onRestore}>
              恢复
            </Button>
          </Show>
          <Button size="sm" variant="ghost" onClick={() => setOpenGroups((v) => !v)}>
            分组 {openGroups() ? "▲" : "▼"}
          </Button>
        </div>

        <Show when={openGroups()}>
          <div class="mt-2 flex flex-wrap items-center gap-3 rounded-control bg-surface-2 p-2">
            <Show
              when={props.allGroups.length > 0}
              fallback={<span class="text-xs text-ink-dim">暂无分组，请先在上方创建分组。</span>}
            >
              <For each={props.allGroups}>
                {(g) => (
                  <label class="flex items-center gap-1 text-xs text-ink">
                    <input
                      type="checkbox"
                      checked={selected().includes(g)}
                      onChange={() => toggleGroup(g)}
                    />
                    {g}
                  </label>
                )}
              </For>
              <Button size="sm" variant="primary" onClick={() => props.onAssignGroups(selected())}>
                应用
              </Button>
            </Show>
          </div>
        </Show>
      </CardContent>
    </Card>
  );
}
