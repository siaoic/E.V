from src.common.logger import get_logger
from src.config.config import config_manager, global_config

logger = get_logger("maisaka_visual_mode")


def _normalize_model_names(model_names: list[str]) -> list[str]:
    """过滤空模型名，保持与模型任务实际回退逻辑一致。"""

    return [model_name.strip() for model_name in model_names if model_name.strip()]


def _resolve_enable_visual_task(task_name: str, fallback_task_name: str = "") -> bool:
    """根据指定任务配置解析当前是否应启用视觉消息。"""

    planner_mode = global_config.visual.planner_mode
    model_config = config_manager.get_model_config()
    model_task_config = model_config.model_task_config
    task_config = getattr(model_task_config, task_name)
    models_by_name = {model.name: model for model in model_config.models}

    if planner_mode == "text":
        return False

    task_models = _normalize_model_names(list(task_config.model_list))
    resolved_task_name = task_name
    if not task_models and fallback_task_name:
        fallback_task_config = getattr(model_task_config, fallback_task_name)
        task_models = _normalize_model_names(list(fallback_task_config.model_list))
        resolved_task_name = fallback_task_name

    task_label = f"{task_name} 任务"
    if resolved_task_name != task_name:
        task_label = f"{task_name} 任务继用的 {resolved_task_name} 任务"

    missing_models = [model_name for model_name in task_models if model_name not in models_by_name]
    non_visual_models = [
        model_name for model_name in task_models if model_name in models_by_name and not models_by_name[model_name].visual
    ]

    if planner_mode == "multimodal":
        if missing_models:
            raise ValueError(f"planner_mode=multimodal，但 {task_label}存在未定义的模型：{', '.join(missing_models)}")
        if non_visual_models:
            raise ValueError(
                f"planner_mode=multimodal，但 {task_label}存在未开启 visual 的模型："
                f"{', '.join(non_visual_models)}"
            )
        return True

    if missing_models:
        logger.warning(
            f"planner_mode=auto 时发现 {task_label}存在未定义模型："
            f"{', '.join(missing_models)}，将退化为纯文本 planner"
        )
        return False

    return bool(task_models) and not non_visual_models


def resolve_enable_visual_planner() -> bool:
    """根据 planner 配置解析当前是否应启用视觉消息。"""

    return _resolve_enable_visual_task("planner")


def resolve_replyer_supports_vision() -> bool:
    """判断回复模型是否已全部支持原生视觉输入。

    Returns:
        bool: 回复任务已配置模型，且这些模型全部开启 visual 时返回 True。
    """

    model_config = config_manager.get_model_config()
    replyer_models = _normalize_model_names(list(model_config.model_task_config.replyer.model_list))
    if not replyer_models:
        return False

    models_by_name = {model.name: model for model in model_config.models}
    for model_name in replyer_models:
        model_info = models_by_name.get(model_name)
        if model_info is None or not model_info.visual:
            return False
    return True


def resolve_need_vlm_image_description() -> bool:
    """根据回复模型是否支持视觉，解析当前是否需要用 VLM 识别图片。

    回复模型自己支持视觉时，图片直接走原生多模态即可，不必再转成文字描述；
    仅当回复模型不支持视觉时，才需要 VLM 兜底生成图片描述。

    Returns:
        bool: 已配置 VLM 任务，且回复模型未能全部支持视觉时返回 True。
    """

    model_config = config_manager.get_model_config()
    vlm_models = model_config.model_task_config.vlm.model_list
    if not any(str(model_name).strip() for model_name in vlm_models):
        return False

    return not resolve_replyer_supports_vision()
