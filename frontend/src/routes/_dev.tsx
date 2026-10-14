import { createSignal, For, Show } from "solid-js";
import { Title } from "@solidjs/meta";
import { Button } from "~/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "~/components/ui/card";
import { Badge, type BadgeTone } from "~/components/ui/badge";
import { TextField } from "~/components/ui/text-field";
import { TextArea } from "~/components/ui/text-area";
import { Select, type SelectOption } from "~/components/ui/select";
import { KSwitch } from "~/components/ui/switch";
import { KSlider } from "~/components/ui/slider";
import { TabsRoot, TabPanel, type TabItem } from "~/components/ui/tabs";
import { DialogRoot } from "~/components/ui/dialog";
import { Stepper, type Step } from "~/components/ui/stepper";
import { notify, Toaster } from "~/components/ui/toast";

// Phase 2 设计系统验收页：驱动全部 ui 组件渲染，校验 Kobalte API 与 Tailwind v4 token 落地。
// 向导切片（Phase 4）达标后可移除。
const FRUIT: SelectOption[] = [
  { value: "apple", label: "苹果" },
  { value: "banana", label: "香蕉" },
  { value: "cherry", label: "樱桃" },
];

const STEPS: Step[] = [
  { id: "content", label: "内容" },
  { id: "visual", label: "视觉" },
  { id: "audio", label: "声音" },
  { id: "output", label: "字幕与生成" },
];

const TABS: TabItem[] = [
  { value: "a", label: "标签 A" },
  { value: "b", label: "标签 B" },
];

const TONES: BadgeTone[] = ["neutral", "primary", "ok", "warn", "err"];

export default function DevShowcase() {
  const [text, setText] = createSignal("你好，世界");
  const [area, setArea] = createSignal("");
  const [fruit, setFruit] = createSignal<string | null>("apple");
  const [on, setOn] = createSignal(true);
  const [volume, setVolume] = createSignal(60);
  const [tab, setTab] = createSignal("a");
  const [step, setStep] = createSignal(1);
  const [maxUnlocked, setMaxUnlocked] = createSignal(2);
  const [dialog, setDialog] = createSignal(false);

  return (
    <main class="mx-auto max-w-4xl space-y-6 px-6 py-10">
      <Title>设计系统 · Showcase</Title>

      <Card>
        <CardHeader>
          <CardTitle>按钮 / 徽章</CardTitle>
        </CardHeader>
        <CardContent class="space-y-3">
          <div class="flex flex-wrap items-center gap-2">
            <Button variant="primary" onClick={() => notify.success("已保存", "演示成功提示")}>
              primary
            </Button>
            <Button variant="secondary">secondary</Button>
            <Button variant="ghost">ghost</Button>
            <Button variant="danger" onClick={() => notify.error("出错了", "演示失败提示")}>
              danger
            </Button>
            <Button size="sm">sm</Button>
            <Button disabled>disabled</Button>
          </div>
          <div class="flex flex-wrap gap-2">
            <For each={TONES}>
              {(t) => <Badge tone={t}>{t}</Badge>}
            </For>
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>表单控件</CardTitle>
        </CardHeader>
        <CardContent class="grid gap-4 sm:grid-cols-2">
          <TextField label="文本" value={text()} onInput={setText} placeholder="请输入" />
          <Select label="水果" value={fruit()} options={FRUIT} onChange={setFruit} />
          <TextArea id="dev-area" label="脚本" value={area()} onInput={setArea} rows={4} />
          <KSlider
            label="音量"
            value={volume()}
            minValue={0}
            maxValue={100}
            onChange={setVolume}
            formatValue={(n) => `${n}%`}
          />
          <KSwitch label="启用" checked={on()} onChange={setOn} />
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>步骤条 / 标签页</CardTitle>
        </CardHeader>
        <CardContent class="space-y-4">
          <Stepper
            steps={STEPS}
            currentIndex={step()}
            maxUnlockedIndex={maxUnlocked()}
            onSelect={(_id, i) => {
              setStep(i);
              setMaxUnlocked((m) => Math.max(m, i));
            }}
          />
          <TabsRoot value={tab()} onChange={setTab} items={TABS}>
            <TabPanel value="a" class="pt-3 text-sm text-ink-dim">
              面板 A 内容
            </TabPanel>
            <TabPanel value="b" class="pt-3 text-sm text-ink-dim">
              面板 B 内容
            </TabPanel>
          </TabsRoot>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>对话框</CardTitle>
        </CardHeader>
        <CardContent>
          <Button variant="primary" onClick={() => setDialog(true)}>
            打开对话框
          </Button>
          <DialogRoot open={dialog()} onOpenChange={setDialog} title="示例对话框">
            <p class="text-sm text-ink-dim">这是一个受控模态，Kobalte 负责焦点陷阱与 Esc 关闭。</p>
            <div class="mt-4 flex justify-end">
              <Button variant="primary" onClick={() => setDialog(false)}>
                知道了
              </Button>
            </div>
          </DialogRoot>
        </CardContent>
      </Card>

      <Show when={fruit()}>
        <p class="text-xs text-ink-dim">当前水果：{fruit()}</p>
      </Show>

      <Toaster />
    </main>
  );
}
