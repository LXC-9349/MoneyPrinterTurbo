PUNCTUATIONS = [
    "?",
    ",",
    ".",
    "、",
    ";",
    ":",
    "!",
    "…",
    "？",
    "，",
    "。",
    "、",
    "；",
    "：",
    "！",
    "...",
    # 阿拉伯语常用标点也应作为自然断句点，避免脚本文本和 edge-tts
    # 返回的字幕停顿边界不一致，导致后续逐行匹配失败。
    "،",
    "؛",
    "؟",
]

TASK_STATE_FAILED = -1
TASK_STATE_COMPLETE = 1
TASK_STATE_PROCESSING = 4
# 工作台任务表/抽屉使用的取消态。现有生成流水线只在阶段边界协作式检查（M3 接入硬
# 中断），这里先给观测层一个稳定取值，与 task_repo._STATE_TO_STATUS[5] 对齐。
TASK_STATE_CANCELLED = 5
# 分阶段确认（里程碑 4 #5）：任务在“脚本生成后/素材获取后”暂停，等待用户在
# 任务详情确认/编辑后继续。与 task_repo._STATE_TO_STATUS[6]='awaiting_confirmation'
# 对齐；属于“进行中”集合（计入顶标“待我确认”计数）。
TASK_STATE_AWAITING_CONFIRMATION = 6

CROSS_POST_STATE_PENDING = "pending"
CROSS_POST_STATE_PROCESSING = "processing"
CROSS_POST_STATE_COMPLETE = "complete"
CROSS_POST_STATE_FAILED = "failed"

FILE_TYPE_VIDEOS = ["mp4", "mov", "mkv", "webm"]
FILE_TYPE_IMAGES = ["jpg", "jpeg", "png", "bmp"]
