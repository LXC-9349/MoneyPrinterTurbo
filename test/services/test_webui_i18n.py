import ast
from test.services.webui_source import parse_all
import json
import re
import unittest
from pathlib import Path

from app.models.llm_provider import get_llm_provider
from app.utils import utils


ROOT_DIR = Path(__file__).parent.parent.parent
WEBUI_MAIN = ROOT_DIR / "webui" / "Main.py"
I18N_DIR = ROOT_DIR / "webui" / "i18n"
LLM_PROVIDER_TIPS_PREFIX = "llm_provider_tips."
TTS_PROVIDER_TIPS_PREFIX = "tts_provider_tips."
SECONDARY_LOCALES = ("az", "de", "es", "fr", "id", "it", "ko", "pt", "ru", "tr", "vi")
PROVIDER_TIPS_PREFIXES = (
    LLM_PROVIDER_TIPS_PREFIX,
    TTS_PROVIDER_TIPS_PREFIX,
)
# 合作 Provider 的品牌名和长说明只维护中英文。次要 locale 统一回退英文，
# 避免把完全相同的品牌名复制十份，也避免长说明后续只更新部分语言。
ENGLISH_FALLBACK_KEYS = frozenset({
    "AI Video Duration Basis Actual",
    "AI Video Duration Basis Estimated",
    "AI Video Material Coverage",
    "AI Video Model",
    "AI Video Model List Load Failed",
    "AI Video Model Reference Price",
    "AI Video Quote Estimate Incomplete",
    "AI Video Quote Required",
    "AI Video Quote Retained For Retry",
    "AI Video Quote Summary",
    "AI Video Quote Summary Singular",
    "AI Video Scene Count",
    "API Platform",
    "Add Person",
    "All Categories",
    "All Dynasties",
    "All Groups",
    "Archive Person",
    "Archived",
    "Article Chars",
    "Base Scripts",
    "Base Scripts Count Selected",
    "Base Scripts Help",
    "Bio Source Note",
    "Bio Step 1 Person",
    "Bio Step 2 Script",
    "Bio Step 3 Settings",
    "Bio Step 4 Generate",
    "Bio Wizard Intro",
    "Biography Script Generation Failed",
    "Biography Video",
    "Changes Saved",
    "Clear Base Scripts",
    "Clear Override",
    "Confirm AI Video Charge",
    "Confirm AI Video Charge Help",
    "Confirm AI Video Charge Required",
    "Confirm Metaso MiniMax Charge",
    "Confirm Metaso MiniMax Charge Help",
    "Confirm Metaso MiniMax Charge Required",
    "Confirm MuAPI Charge",
    "Confirm MuAPI Charge Help",
    "Confirm MuAPI Charge Required",
    "Content Category",
    "Create Group",
    "Create Person",
    "Custom API Endpoint",
    "Custom Dynasty",
    "Custom Group",
    "Custom Person",
    "Delete Group",
    "Delete Person",
    "Dynasty",
    "Dynasty Name",
    "Dynasty Override",
    "Edit Person",
    "Facts One Per Line Help",
    "Filtered Person Count",
    "First Run Onboarding Desc",
    "First Run Onboarding Dismiss",
    "First Run Onboarding Open Settings",
    "First Run Onboarding Title",
    "Generation Mode",
    "Generation Mode Help",
    "Group Created",
    "Group Exists",
    "Group Name",
    "Groups",
    "Historical Archive Help",
    "Keep Current",
    "Key Facts",
    "Library Person Count",
    "Local LLM Script Generation",
    "Local Library Not Built",
    "Local Library Unavailable",
    "LoomLoom Poll Retry Pending",
    "LoomLoom Poll Retry Warning",
    "LoomLoom Quote Summary Singular",
    "LoomLoom Video Terms Reuse Help",
    "Manage Groups",
    "Members",
    "Metaso MiniMax API Key",
    "Metaso MiniMax API Key Help",
    "Metaso MiniMax Base URL",
    "Metaso MiniMax Billing Notice",
    "Metaso MiniMax Billing Notice Uploaded Audio",
    "Metaso MiniMax Billing Notice Without Script",
    "Metaso MiniMax H3",
    "Metaso MiniMax H3 Help",
    "Metaso MiniMax Invalid Resolution",
    "Metaso MiniMax Resolution",
    "Metaso MiniMax Resolution Help",
    "MuAPI AI Video",
    "MuAPI AI Video Help",
    "MuAPI API Key",
    "MuAPI API Key Help",
    "MuAPI Base URL",
    "MuAPI Base URL Help",
    "MuAPI Billing Notice",
    "MuAPI Billing Notice Without Script",
    "MuAPI Resolution",
    "MuAPI Resolution Help",
    "MuAPI Video Endpoint",
    "MuAPI Video Endpoint Help",
    "No Person Found",
    "None (Animation)",
    "Normal Video",
    "Note",
    "Person Added",
    "Person Name",
    "Person Selected",
    "Pin Person",
    "Please Enter Group Name",
    "Please Enter Person Name",
    "Please Enter the Metaso MiniMax API Key",
    "Please Enter the MuAPI API Key",
    "Read Full Text",
    "Reference Books",
    "Refresh AI Video Models",
    "Restore Person",
    "Resume LoomLoom Status Check",
    "Retry AI Video Quote",
    "Save Changes",
    "Script Generation Method",
    "Script Generation Method Help",
    "Search Person",
    "Select All Base Scripts",
    "Select Metaso MiniMax Resolution",
    "Select Person",
    "Selected AI Video Model Unavailable",
    "Selected AI Video Ratio Unavailable",
    "Shengsuan Cloud AI Video",
    "Shengsuan Cloud AI Video Help",
    "Shengsuan Cloud API Key",
    "Shengsuan Cloud API Key Help",
    "Shengsuan Cloud API Key Link",
    "Shengsuan Cloud API Key Placeholder",
    "Shengsuan Cloud API Key Required",
    "Shengsuan Cloud API Key Reused",
    "Shengsuan Cloud Batch Script Generation",
    "Show Archived",
    "Stop Tracking LoomLoom Run",
    "Stop Tracking LoomLoom Run Help",
    "Unavailable AI Video Model",
    "Unpin Person",
    "Use This Person",
    "VoxCPM Speed Not Supported",
    "admin_menu_generate",
    "admin_menu_library",
    "admin_menu_persons",
    "admin_menu_storage",
    "admin_menu_tasks",
    "admin_page_back",
    "admin_page_hint",
    "admin_page_missing",
    "admin_sidebar_hint",
    "admin_sidebar_settings",
    "articles",
    "llm_provider_authentication_error.moonshot",
    "llm_provider_endpoint.moonshot.china",
    "llm_provider_endpoint.moonshot.global",
    "llm_provider_endpoint_selector.moonshot",
    "llm_provider_endpoint_selector_help.moonshot",
    "llm_provider_label.api_route",
    "llm_provider_label.apimart",
    "llm_provider_label.fluxionai",
    "llm_provider_label.openrouter",
    "llm_provider_label.shengsuanyun",
    "projects_aspect",
    "projects_back",
    "projects_copy_params",
    "projects_date",
    "projects_delete",
    "projects_delete_confirm",
    "projects_delete_failed",
    "projects_deleted",
    "projects_detail",
    "projects_empty",
    "projects_export",
    "projects_export_missing",
    "projects_hint",
    "projects_import_task_missing",
    "projects_import_tasks",
    "projects_imported",
    "projects_filter_mode",
    "projects_filter_status",
    "projects_filter_all",
    "admin_menu_overview",
    "ov_hint",
    "ov_metric_total",
    "ov_metric_done",
    "ov_metric_running",
    "ov_metric_missing",
    "ov_coverage",
    "ov_filter_status",
    "ov_filter_dynasty",
    "ov_filter_category",
    "ov_search",
    "ov_search_ph",
    "ov_refresh",
    "ov_status_all",
    "ov_status_done",
    "ov_status_missing",
    "ov_status_running",
    "ov_status_failed",
    "ov_badge_done",
    "ov_badge_running",
    "ov_badge_failed",
    "ov_badge_missing",
    "ov_last",
    "ov_action_generate",
    "ov_action_regenerate",
    "ov_empty",
    "ov_per_page",
    "ov_page",
    "ov_showing",
    "ov_unregistered",
    "ov_col_status",
    "ov_col_name",
    "ov_col_meta",
    "ov_col_articles",
    "ov_col_last",
    "ov_col_action",
    "projects_quality_ok",
    "projects_quality_warn",
    "projects_content_ok",
    "projects_content_warn",
    "projects_regen_suggest",
    "projects_tags",
    "projects_tags_label",
    "projects_tags_help",
    "projects_tags_save",
    "projects_tags_done",
    "projects_rename",
    "projects_rename_label",
    "projects_rename_save",
    "projects_rename_done",
    "projects_rename_failed",
    "projects_sort",
    "projects_sort_time",
    "projects_sort_title",
    "projects_sort_duration",
    "projects_sort_size",
    "projects_select",
    "projects_batch_delete",
    "projects_batch_deleted",
    "projects_selected",
    "admin_nav_brand",
    "projects_language",
    "projects_menu",
    "projects_mode",
    "projects_mode_biography",
    "projects_mode_normal",
    "projects_none_done",
    "projects_open_dir",
    "projects_params_snapshot",
    "projects_person",
    "projects_play",
    "projects_regen_done",
    "projects_regen_error",
    "projects_regenerate",
    "projects_regenerate_hint",
    "projects_script",
    "projects_script_none",
    "projects_search",
    "projects_search_ph",
    "projects_size",
    "projects_source",
    "projects_status",
    "projects_status_done",
    "projects_status_failed",
    "projects_status_missing",
    "projects_status_running",
    "projects_subject",
    "projects_title",
    "projects_title_empty",
    "projects_total",
    "settings",
    "admin_library_hint",
    "admin_library_none",
    "admin_library_profile_docs",
    "admin_library_profile_dynasty",
    "admin_library_profile_facts",
    "admin_library_search",
    "admin_library_search_btn",
    "admin_library_search_ph",
    "admin_library_search_required",
    "admin_library_unavailable",
    "admin_persons_apply_groups",
    "admin_persons_archived",
    "admin_persons_category",
    "admin_persons_create",
    "admin_persons_create_failed",
    "admin_persons_create_group",
    "admin_persons_created",
    "admin_persons_delete",
    "admin_persons_delete_failed",
    "admin_persons_delete_group",
    "admin_persons_delete_group_of",
    "admin_persons_deleted",
    "admin_persons_dynasty",
    "admin_persons_edit",
    "admin_persons_edit_title",
    "admin_persons_facts",
    "admin_persons_group_create_failed",
    "admin_persons_group_created",
    "admin_persons_group_deleted",
    "admin_persons_groups",
    "admin_persons_groups_current",
    "admin_persons_groups_for",
    "admin_persons_groups_saved",
    "admin_persons_hint",
    "admin_persons_more",
    "admin_persons_name",
    "admin_persons_name_ph",
    "admin_persons_new",
    "admin_persons_new_group",
    "admin_persons_note",
    "admin_persons_pin",
    "admin_persons_pin_toggled",
    "admin_persons_save",
    "admin_persons_saved",
    "admin_persons_search",
    "admin_persons_search_ph",
    "admin_persons_show_hidden",
    "admin_persons_total",
    "admin_persons_unpin",
    "admin_persons_source_custom",
    "admin_persons_source_library",
    "admin_storage_dir_missing",
    "admin_storage_hint",
    "admin_storage_open_root",
    "admin_storage_total",
    "admin_storage_dir_tasks",
    "admin_storage_dir_cache_videos",
    "admin_storage_dir_local_videos",
    "admin_storage_dir_cache_material_search",
    "admin_storage_dir_bgm",
    "admin_storage_dir_library",
    "admin_video_cache_30",
    "admin_video_cache_7",
    "admin_video_cache_90",
    "admin_video_cache_all",
    "admin_video_cache_clean",
    "admin_video_cache_confirm",
    "admin_video_cache_count",
    "admin_video_cache_dir",
    "admin_video_cache_done",
    "admin_video_cache_fail",
    "admin_video_cache_oldest",
    "admin_video_cache_preview",
    "admin_video_cache_range",
    "admin_video_cache_refresh",
    "admin_video_cache_size",
    "admin_task_complete",
    "admin_task_copy",
    "admin_task_copy_none",
    "admin_task_delete",
    "admin_task_delete_failed",
    "admin_task_deleted",
    "admin_task_failed",
    "admin_task_failure_details",
    "admin_task_failure_no_detail",
    "admin_task_failure_progress",
    "admin_task_failure_reason",
    "admin_task_failure_stage",
    "admin_task_failure_unknown",
    "admin_task_history",
    "admin_task_open",
    "admin_task_play",
    "admin_task_processing",
    "admin_task_view_script",
    "admin_tasks_all",
    "admin_tasks_complete",
    "admin_tasks_empty",
    "admin_tasks_failed",
    "admin_tasks_hint",
    "admin_tasks_metric_complete",
    "admin_tasks_metric_failed",
    "admin_tasks_metric_processing",
    "admin_tasks_metric_total",
    "admin_tasks_open_root",
    "admin_tasks_processing",
    "admin_tasks_search",
    "admin_tasks_search_ph",
    "admin_tasks_status",
    "admin_task_stage_preflight",
    "admin_task_stage_script",
    "admin_task_stage_terms",
    "admin_task_stage_audio",
    "admin_task_stage_video",
    "admin_task_stage_materials",
    "admin_task_stage_pipeline",
    "admin_task_stage_dequeue",
    "admin_task_stage_scheduling",
    "admin_task_stage_webui_worker",
    "admin_task_failed_stage",
    "admin_storage_dir_open",
    "admin_storage_file_count",
    "admin_persons_selected_groups",
    "admin_storage_dir_label",
    "admin_storage_missing",
    "admin_storage_dir_open",
    "admin_storage_file_count",
    "admin_persons_selected_groups",
    "admin_storage_dir_label",
    "admin_storage_missing",

    "admin_task_retry",
    "admin_task_regenerate",
    })
FORMAT_PLACEHOLDER_PATTERN = re.compile(r"(?<!\{)\{([a-zA-Z_][a-zA-Z0-9_]*)\}(?!\})")
MARKDOWN_URL_PATTERN = re.compile(r"\[[^\]]+\]\((https?://[^)]+)\)")


class _TrKeyVisitor(ast.NodeVisitor):
    def __init__(self):
        self.keys = set()

    def visit_Call(self, node):
        if (
            isinstance(node.func, ast.Name)
            and node.func.id == "tr"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            self.keys.add(node.args[0].value)
        self.generic_visit(node)


def _load_translation(locale):
    data = json.loads((I18N_DIR / f"{locale}.json").read_text(encoding="utf-8"))
    return data.get("Translation", {})


def _duplicate_translation_keys(path):
    """返回 locale 原始文本中重复定义的键，JSON 解析只保留最后一个。"""
    duplicates = []

    def collect(pairs):
        seen = set()
        for key, _ in pairs:
            if key in seen:
                duplicates.append(key)
            seen.add(key)
        return dict(pairs)

    json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=collect)
    return duplicates


def _required_translation_keys(translations):
    """返回二级语言必须维护的 key，Provider 长说明统一回退英文。"""
    return {
        key
        for key in translations
        if key not in ENGLISH_FALLBACK_KEYS
        and not key.startswith(PROVIDER_TIPS_PREFIXES)
    }


def _format_placeholders(value):
    """提取运行时格式化变量，防止翻译遗漏或误改变量名。"""
    return set(FORMAT_PLACEHOLDER_PATTERN.findall(value))


def _markdown_urls(value):
    """提取 Markdown 链接目标，允许翻译链接文字但不允许改坏地址。"""
    return set(MARKDOWN_URL_PATTERN.findall(value))


class TestWebuiI18n(unittest.TestCase):
    def test_saved_ui_language_takes_priority_over_browser_locale(self):
        language = utils.resolve_ui_language(
            saved_language="de",
            browser_locale="zh-CN",
            supported_languages=["zh", "en", "de"],
        )

        self.assertEqual(language, "de")

    def test_browser_locale_is_normalized_to_supported_base_language(self):
        self.assertEqual(
            utils.resolve_ui_language("", "zh-CN", ["zh", "en"]),
            "zh",
        )
        self.assertEqual(
            utils.resolve_ui_language(None, "pt_BR", ["en", "pt"]),
            "pt",
        )

    def test_unsupported_browser_locale_falls_back_to_english(self):
        language = utils.resolve_ui_language(
            saved_language="",
            browser_locale="fr-FR",
            supported_languages=["zh", "en"],
        )

        self.assertEqual(language, "en")

    def test_english_locale_covers_static_webui_labels(self):
        tree = parse_all()
        visitor = _TrKeyVisitor()
        visitor.visit(tree)

        en_keys = set(_load_translation("en"))

        self.assertEqual(sorted(visitor.keys - en_keys), [])

    def test_shengsuanyun_provider_tips_keep_registration_and_model_links(self):
        """合作入口和模型目录属于产品配置，避免后续改文案时误删追踪链接。"""
        expected_urls = {
            "https://www.shengsuanyun.com/?from=CH_XUQ4OTSK",
            "https://global.modelmesh.info/model",
        }

        for locale in ("zh", "en"):
            with self.subTest(locale=locale):
                tips = _load_translation(locale)["llm_provider_tips.shengsuanyun"]
                provider = get_llm_provider("shengsuanyun")
                rendered = tips.format(
                    api_key_url=provider.effective_api_key_url(),
                    default_base_url=provider.effective_default_base_url,
                    default_model=provider.default_model,
                )
                self.assertEqual(_markdown_urls(rendered), expected_urls)

    def test_metaso_api_key_label_keeps_mpt_referral_link(self):
        """秘塔 Key 获取入口必须保留 MPT 追踪参数，避免赞助转化链路失效。"""
        expected_url = "https://metaso.cn/minimax-h3/?s=MPT"

        for locale in ("zh", "en"):
            with self.subTest(locale=locale):
                label = _load_translation(locale)["Metaso MiniMax API Key"]
                self.assertEqual(_markdown_urls(label), {expected_url})

    def test_secondary_locales_cover_english_locale(self):
        en_translations = _load_translation("en")
        required_en_keys = _required_translation_keys(en_translations)

        for locale in SECONDARY_LOCALES:
            with self.subTest(locale=locale):
                locale_keys = set(_load_translation(locale))
                self.assertEqual(sorted(required_en_keys - locale_keys), [])

    def test_secondary_locales_do_not_duplicate_provider_tips(self):
        # Provider 配置长说明只维护中英文，其它语言运行时回退英文。
        # 禁止复制这些 key，避免出现不会持续维护的半翻译内容。
        for locale in SECONDARY_LOCALES:
            with self.subTest(locale=locale):
                locale_keys = set(_load_translation(locale))
                duplicated_keys = sorted(
                    key for key in locale_keys if key.startswith(PROVIDER_TIPS_PREFIXES)
                )
                self.assertEqual(duplicated_keys, [])

    def test_secondary_locales_do_not_duplicate_english_fallback_keys(self):
        for locale in SECONDARY_LOCALES:
            with self.subTest(locale=locale):
                locale_keys = set(_load_translation(locale))
                self.assertEqual(sorted(ENGLISH_FALLBACK_KEYS & locale_keys), [])

    def test_secondary_locales_cover_static_webui_labels(self):
        tree = parse_all()
        visitor = _TrKeyVisitor()
        visitor.visit(tree)

        for locale in SECONDARY_LOCALES:
            with self.subTest(locale=locale):
                locale_keys = set(_load_translation(locale))
                self.assertEqual(
                    sorted(visitor.keys - locale_keys - ENGLISH_FALLBACK_KEYS),
                    [],
                )

    def test_secondary_locales_preserve_format_placeholders(self):
        en_translations = _load_translation("en")

        for locale in SECONDARY_LOCALES:
            locale_translations = _load_translation(locale)
            for key in _required_translation_keys(en_translations):
                with self.subTest(locale=locale, key=key):
                    self.assertEqual(
                        _format_placeholders(locale_translations[key]),
                        _format_placeholders(en_translations[key]),
                    )

    def test_secondary_locales_preserve_markdown_urls(self):
        en_translations = _load_translation("en")

        for locale in SECONDARY_LOCALES:
            locale_translations = _load_translation(locale)
            for key in _required_translation_keys(en_translations):
                with self.subTest(locale=locale, key=key):
                    self.assertEqual(
                        _markdown_urls(locale_translations[key]),
                        _markdown_urls(en_translations[key]),
                    )

    def test_script_language_options_include_russian(self):
        tree = parse_all()
        support_locales = None

        for node in tree.body:
            if not isinstance(node, ast.Assign):
                continue
            if any(
                isinstance(target, ast.Name) and target.id == "support_locales"
                for target in node.targets
            ):
                support_locales = ast.literal_eval(node.value)
                break

        self.assertIsNotNone(support_locales)
        self.assertIn("ru-RU", support_locales)

    def test_locale_files_do_not_redefine_a_translation_key(self):
        """
        同一 JSON 对象里出现重复键时，解析只保留最后一个，前一个被静默丢弃。
        视频转场与字幕动画曾共用 "None" 键，中文转场下拉因此显示成「无动画」。
        这里直接检查原始 locale 文本，避免同类覆盖再次逃过 tr() 键覆盖测试。
        """

        for path in sorted(I18N_DIR.glob("*.json")):
            with self.subTest(locale=path.stem):
                self.assertEqual(_duplicate_translation_keys(path), [])

