"""传记视频端到端示例脚本（演示用，非生产入口）。

用途：串联 library → llm → video 服务，演示如何以「曹操」为例跑通
一条从人物档案 → 结构化脚本 → 历史档案素材 → 成片的传记流水线。
正式出片请走 SolidStart 前端 / FastAPI `/api/v1/videos`；本脚本仅作
最小可运行示例与手动冒烟验证，直接运行：
    uv run python scripts/example_biography_video.py
（需先构建本地史料库并配好 ffmpeg，见 docs/QUICKSTART.md）
"""
import json
from app.services import library, llm, video, voice, subtitle, material

print("=" * 80)
print("🎬 高质量传记视频生成器")
print("=" * 80)

# 1. 选择人物
person_name = "曹操"
print(f"\n📌 生成人物：{person_name}")

# 2. 获取人物档案（Grounding）
print("\n📚 读取人物档案...")
profile = library.get_person_profile(person_name)

if not profile:
    print(f"❌ 未找到 '{person_name}' 的档案，请检查数据库")
    exit(1)

print(f"✅ 找到 {profile['doc_count']} 篇史料文章")
print(f"✅ 时代：{profile['dynasty']}")

# 3. 生成结构化脚本（使用 Grounding）
print("\n✍️ 生成结构化脚本...")
grounding = library.grounding_block(person_name, max_facts=15)

script_prompt = f"""
你是一位专业的历史纪录片编剧。请根据以下事实为'{person_name}'生成一个 3 段式的视频脚本。

【核心事实 - 必须严格基于这些事实】
{grounding}

【脚本要求】
1. 总共 3 段，每段 120-150 字
2. 第一段：开场钩子 + 生平背景（吸引观众）
3. 第二段：核心成就/事件（重点内容）
4. 第三段：历史评价 + 现代意义（升华主题）
5. 语言风格：通俗易懂、生动有趣、避免学术化
6. 每段之间用两个换行符分隔

【输出格式】
只输出脚本正文，不要任何解释或标记。
"""

# 调用 LLM 生成脚本
script_response = llm.chat([
    {"role": "system", "content": "你是一位专业的历史纪录片编剧，擅长将复杂的历史事件转化为通俗易懂的视频脚本。"},
    {"role": "user", "content": script_prompt}
])

script = script_response.choices[0].message.content if hasattr(script_response, 'choices') else str(script_response)
print("✅ 脚本生成完成")
print("\n📝 脚本预览:")
print("-" * 80)
print(script[:300] + "...")
print("-" * 80)

# 4. 准备视频参数
print("\n⚙️ 配置视频参数...")
video_params = {
    "video_subject": person_name,
    "video_script": script,
    "video_source": "historical_archive",  # 使用历史档案源
    "paragraph_number": 3,
    "video_aspect": "16:9",
    "voice_name": "mimo:白桦-Male",  # 使用 MiMo TTS
    "voice_rate": 1.0,
    "voice_volume": 1.0,
    "subtitle_enabled": True,
    "font_name": "MicrosoftYaHeiBold.ttc",
    "font_size": 70,
    "bgm_type": "random",
    "bgm_volume": 0.15,
    "match_materials_to_script": True,
    "video_clip_duration": 5,
    "video_transition_mode": "FadeIn",
}

print(f"✅ 视频画幅：{video_params['video_aspect']}")
print(f"✅ 配音：{video_params['voice_name']}")
print(f"✅ 素材源：历史档案（真实照片）")

# 5. 开始生成视频
print("\n🎬 开始生成视频...")
print("   这可能需要几分钟，请耐心等待...")

try:
    # 调用视频生成服务
    task_result = video.generate_video(video_params)
    
    print("\n✅ 视频生成成功！")
    print(f"\n📊 任务信息:")
    print(f"   任务 ID: {task_result.get('task_id', 'N/A')}")
    print(f"   视频路径：{task_result.get('video_path', 'N/A')}")
    print(f"   音频路径：{task_result.get('audio_path', 'N/A')}")
    print(f"   字幕路径：{task_result.get('subtitle_path', 'N/A')}")
    
    # 显示合规信息
    compliance_path = task_result.get('compliance_path', '')
    if compliance_path:
        print(f"\n📋 合规说明文件：{compliance_path}")
        
except Exception as e:
    print(f"\n❌ 视频生成失败：{e}")
    import traceback
    traceback.print_exc()

print("\n" + "=" * 80)
print("✨ 生成完成！")
print("=" * 80)
