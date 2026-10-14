import json

MATERIALS = [
    "img-1e682162df6668ae1f0617b46975ae43.jpg.mp4",
    "img-379b86bee6b7c69080706d42745a7970.jpg.mp4",
    "img-5c413bbbdc36586315bc5a342c1528fc.jpg.mp4",
    "img-8aedced8381f84e3d1b2113040dbb378.jpg.mp4",
    "img-a49d1621948e1fdd5a40e88eecc5519f.jpg.mp4",
    "img-aa987f1b924f1fb32c63edc6fe49d3a1.jpg.mp4",
    "img-aed53406b6fb1d1c6775d7c6071e4341.jpg.mp4",
    "img-c3dc7c4ee3c06991f553de4205d47f40.jpg.mp4",
    "img-c6adc5b792917edf294c1aaf0f0dcd36.jpg.mp4",
]

task = {
    "video_subject": "城市清晨",
    "video_script": (
        "当第一缕晨光穿透薄雾，城市还在沉睡。"
        "街道上，早起的行人踏出了第一步。"
        "一杯温热的咖啡，开启了忙碌的一天。"
        "车流与人流交织，生活在晨光中重新出发。"
        "每一个平凡的清晨，都藏着不平凡的希望。"
    ),
    "video_source": "local",
    "video_language": "zh-CN",
    "video_aspect": "9:16",
    "video_count": 1,
    "voice_name": "zh-CN-XiaoxiaoNeural-Female",
    "voice_rate": 1.0,
    "voice_volume": 1.0,
    "subtitle_enabled": True,
    "subtitle_position": "bottom",
    "font_name": "STHeitiMedium.ttc",
    "text_fore_color": "#FFFFFF",
    "stroke_color": "#000000",
    "stroke_width": 1.5,
    "font_size": 60,
    "bgm_type": "random",
    "bgm_volume": 0.2,
    "video_clip_duration": 3,
    "video_concat_mode": "sequential",
    "video_transition_mode": "FadeIn",
    "video_fit_mode": "cover",
    "video_materials": [
        {"provider": "local", "url": "storage/local_videos/" + name}
        for name in MATERIALS
    ],
}

with open("E:/pj-vd/MoneyPrinterTurbo/_verify_task.json", "w", encoding="utf-8") as f:
    json.dump([task], f, ensure_ascii=False, indent=2)
print("manifest written")
