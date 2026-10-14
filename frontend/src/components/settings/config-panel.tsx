// 全局设置表单面板：读写 config.app / config.ui 的必需子集。
// 含 secret（api_key / provider key），因此一律在客户端 onMount 拉取、直连写回，
// 不经 SSR 注入 HTML。字段用 createStore 建模，逐字段 signal 更新，保存后以
// 后端权威响应回填（base_url/model 命中默认值会被后端归一化为空，输入框留空即为默认）。
import { createSignal, onMount, Show } from "solid-js";
import { createStore } from "solid-js/store";
import { Button } from "~/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "~/components/ui/card";
import { Select } from "~/components/ui/select";
import { TextField } from "~/components/ui/text-field";
import { KSwitch } from "~/components/ui/switch";
import { notify } from "~/components/ui/toast";
import { ApiError, fetchSettings, testLlmConnection, updateSettings } from "~/lib/api/client";
import type { LlmProviderMeta, SettingsUpdateInput } from "~/lib/api/types";

interface FormState extends SettingsUpdateInput {}

const EMPTY: FormState = {
  llm_provider: "",
  llm_api_key: "",
  llm_base_url: "",
  llm_model_name: "",
  pexels_api_keys: "",
  pixabay_api_keys: "",
  coverr_api_keys: "",
  api_key: "",
  hide_log: false,
  upload_post_enabled: false,
  upload_post_auto_upload: false,
  upload_post_api_key: "",
  upload_post_username: "",
  upload_post_youtube_privacy_status: "public",
  upload_post_youtube_made_for_kids: false,
};

const PRIVACY_OPTIONS = [
  { value: "public", label: "公开" },
  { value: "unlisted", label: "不公开（凭链接）" },
  { value: "private", label: "私有" },
];

function errMsg(e: unknown): string {
  return e instanceof ApiError ? e.message : String(e);
}

export function SettingsConfigPanel() {
  const [form, setForm] = createStore<FormState>(EMPTY);
  const [providers, setProviders] = createSignal<LlmProviderMeta[]>([]);
  const [effectiveBaseUrl, setEffectiveBaseUrl] = createSignal("");
  const [effectiveModel, setEffectiveModel] = createSignal("");
  const [loading, setLoading] = createSignal(true);
  const [saving, setSaving] = createSignal(false);
  const [testing, setTesting] = createSignal(false);
  const [switching, setSwitching] = createSignal(false);

  // 用后端视图回填表单：override 为空表示沿用默认，展示时留空（占位提示默认值）。
  function applyView(v: Awaited<ReturnType<typeof fetchSettings>>) {
    setForm({
      llm_provider: v.llm.provider,
      llm_api_key: v.llm.api_key,
      llm_base_url: v.llm.base_url_override,
      llm_model_name: v.llm.model_override,
      pexels_api_keys: v.material.pexels_api_keys,
      pixabay_api_keys: v.material.pixabay_api_keys,
      coverr_api_keys: v.material.coverr_api_keys,
      api_key: v.api_key,
      hide_log: v.ui.hide_log,
      upload_post_enabled: v.upload_post.enabled,
      upload_post_auto_upload: v.upload_post.auto_upload,
      upload_post_api_key: v.upload_post.api_key,
      upload_post_username: v.upload_post.username,
      upload_post_youtube_privacy_status: v.upload_post.youtube_privacy_status,
      upload_post_youtube_made_for_kids: v.upload_post.youtube_made_for_kids,
    });
    setProviders(v.llm_providers);
    setEffectiveBaseUrl(v.llm.base_url_effective);
    setEffectiveModel(v.llm.model_effective);
  }

  const providerOptions = () => providers().map((p) => ({ value: p.id, label: p.label }));
  const currentProvider = () => providers().find((p) => p.id === form.llm_provider) ?? null;

  async function load() {
    setLoading(true);
    try {
      applyView(await fetchSettings());
    } catch (e) {
      notify.error("读取设置失败", errMsg(e));
    } finally {
      setLoading(false);
    }
  }
  onMount(load);

  async function save() {
    setSaving(true);
    try {
      applyView(await updateSettings({ ...form }));
      notify.success("设置已保存");
    } catch (e) {
      notify.error("保存失败", errMsg(e));
    } finally {
      setSaving(false);
    }
  }

  async function test() {
    setTesting(true);
    try {
      const r = await testLlmConnection();
      if (r.ok) notify.success(`连接正常（${r.elapsed.toFixed(2)}s）`);
      else notify.error("连接失败", r.error);
    } catch (e) {
      notify.error("连接测试异常", errMsg(e));
    } finally {
      setTesting(false);
    }
  }

  // Provider 切换即刻落盘（仅写 llm_provider 键，不影响其它未保存字段），
  // 再回读该 Provider 已存的密钥/BaseURL/模型，避免把上一个 Provider 的凭证
  // 字段带入新 Provider 的 config 键（与 Streamlit 设置弹窗语义一致）。
  async function onProviderChange(next: string | null) {
    const id = (next ?? "").trim();
    if (!id || id === form.llm_provider) return;
    setSwitching(true);
    try {
      applyView(await updateSettings({ llm_provider: id }));
      notify.info(`已切换到 ${id}，请确认其 API Key`);
    } catch (e) {
      notify.error("切换 Provider 失败", errMsg(e));
    } finally {
      setSwitching(false);
    }
  }

  return (
    <Show
      when={!loading()}
      fallback={<p class="py-8 text-center text-sm text-ink-dim">加载设置中…</p>}
    >
      <div class="flex flex-col gap-5">
        {/* LLM Provider */}
        <Card>
          <CardHeader>
            <CardTitle>LLM 模型服务</CardTitle>
          </CardHeader>
          <CardContent class="flex flex-col gap-3">
            <Select
              label="Provider"
              value={form.llm_provider ?? null}
              options={providerOptions()}
              onChange={onProviderChange}
              placeholder="选择 Provider"
              disabled={switching()}
            />
            <Show when={currentProvider()?.show_api_key}>
              <TextField
                label="API Key"
                type="password"
                value={form.llm_api_key ?? ""}
                onInput={(v) => setForm("llm_api_key", v)}
                placeholder="粘贴该 Provider 的密钥"
              />
            </Show>
            <Show when={currentProvider()?.show_base_url}>
              <TextField
                label="Base URL（可选覆盖）"
                value={form.llm_base_url ?? ""}
                onInput={(v) => setForm("llm_base_url", v)}
                placeholder={effectiveBaseUrl() || "默认"}
                description={`当前生效：${effectiveBaseUrl() || "—"}`}
              />
            </Show>
            <Show when={currentProvider()?.requires_model_name}>
              <TextField
                label="模型名（可选覆盖）"
                value={form.llm_model_name ?? ""}
                onInput={(v) => setForm("llm_model_name", v)}
                placeholder={effectiveModel() || "默认"}
                description={`当前生效：${effectiveModel() || "—"}`}
              />
            </Show>
            <div class="flex items-center gap-2">
              <Button variant="secondary" size="sm" onClick={test} disabled={testing()}>
                {testing() ? "测试中…" : "测试连接"}
              </Button>
              <Show when={currentProvider()?.api_key_url}>
                {(url) => (
                  <a
                    class="text-xs text-primary hover:underline"
                    href={url()}
                    target="_blank"
                    rel="noreferrer"
                  >
                    申请 API Key ↗
                  </a>
                )}
              </Show>
            </div>
          </CardContent>
        </Card>

        {/* 素材 API Key */}
        <Card>
          <CardHeader>
            <CardTitle>素材源 API Key</CardTitle>
          </CardHeader>
          <CardContent class="flex flex-col gap-3">
            <TextField
              label="Pexels（逗号分隔多个）"
              value={form.pexels_api_keys ?? ""}
              onInput={(v) => setForm("pexels_api_keys", v)}
              placeholder="key1, key2"
            />
            <TextField
              label="Pixabay（逗号分隔多个）"
              value={form.pixabay_api_keys ?? ""}
              onInput={(v) => setForm("pixabay_api_keys", v)}
              placeholder="key1, key2"
            />
            <TextField
              label="Coverr（逗号分隔多个）"
              value={form.coverr_api_keys ?? ""}
              onInput={(v) => setForm("coverr_api_keys", v)}
              placeholder="key1, key2"
            />
          </CardContent>
        </Card>

        {/* 界面与鉴权 */}
        <Card>
          <CardHeader>
            <CardTitle>界面与接口</CardTitle>
          </CardHeader>
          <CardContent class="flex flex-col gap-3">
            <KSwitch
              label="隐藏运行日志"
              checked={form.hide_log ?? false}
              onChange={(v) => setForm("hide_log", v)}
            />
            <TextField
              label="接口鉴权 Key（api_key，留空则不校验）"
              type="password"
              value={form.api_key ?? ""}
              onInput={(v) => setForm("api_key", v)}
              placeholder="设置后调用 /api/v1 需携带"
            />
          </CardContent>
        </Card>

        {/* Upload-Post 自动发布 */}
        <Card>
          <CardHeader>
            <CardTitle>自动发布（Upload-Post）</CardTitle>
          </CardHeader>
          <CardContent class="flex flex-col gap-3">
            <div class="flex gap-6">
              <KSwitch
                label="启用"
                checked={form.upload_post_enabled ?? false}
                onChange={(v) => setForm("upload_post_enabled", v)}
              />
              <KSwitch
                label="生成后自动上传"
                checked={form.upload_post_auto_upload ?? false}
                onChange={(v) => setForm("upload_post_auto_upload", v)}
              />
            </div>
            <TextField
              label="Upload-Post API Key"
              type="password"
              value={form.upload_post_api_key ?? ""}
              onInput={(v) => setForm("upload_post_api_key", v)}
            />
            <TextField
              label="用户名"
              value={form.upload_post_username ?? ""}
              onInput={(v) => setForm("upload_post_username", v)}
            />
            <Select
              label="YouTube 可见性"
              value={form.upload_post_youtube_privacy_status ?? "public"}
              options={PRIVACY_OPTIONS}
              onChange={(v) => setForm("upload_post_youtube_privacy_status", v ?? "public")}
            />
            <KSwitch
              label="标记为面向儿童（YouTube madeForKids）"
              checked={form.upload_post_youtube_made_for_kids ?? false}
              onChange={(v) => setForm("upload_post_youtube_made_for_kids", v)}
            />
          </CardContent>
        </Card>

        <div class="flex justify-end">
          <Button variant="primary" onClick={save} disabled={saving()}>
            {saving() ? "保存中…" : "保存设置"}
          </Button>
        </div>
      </div>
    </Show>
  );
}
