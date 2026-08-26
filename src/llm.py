"""LLM 封装：基于 OpenAI 兼容 API。无 Key 时自动降级。"""
from __future__ import annotations

import json
import re
from typing import List, Dict, Any, Optional

from .config import Config


def _extract_json(text: str):
    """从模型回复中抽取 JSON（兼容 ```json 代码块）。"""
    text = text.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if m:
        text = m.group(1)
    # 取第一个 [ 或 { 到最后一个 ] 或 }
    s = re.search(r"[\[{]", text)
    e = re.search(r"[\]}]", text)
    if s and e:
        text = text[s.start():e.end() + 1]
    return json.loads(text)


class LLM:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.client = None
        if cfg.use_llm and cfg.llm_api_key:
            try:
                from openai import OpenAI
                self.client = OpenAI(api_key=cfg.llm_api_key,
                                     base_url=cfg.llm_base_url)
            except Exception as e:  # noqa
                print(f"[warn] LLM 初始化失败，将使用降级方案: {e}")

    def available(self) -> bool:
        return self.client is not None

    def chat_json(self, prompt: str, temperature: float = 0) -> Any:
        """通用 JSON 抽取调用；失败返回 None（交由调用方回退）。"""
        if not self.available():
            return None
        try:
            resp = self.client.chat.completions.create(
                model=self.cfg.llm_model,
                messages=[{"role": "user", "content": prompt}],
                temperature=temperature,
            )
            return _extract_json(resp.choices[0].message.content)
        except Exception as e:  # noqa
            print(f"[warn] LLM 调用失败: {e}")
            return None

    def classify_image(self, prompt: str, image_path: str) -> Optional[Dict[str, Any]]:
        """视觉分类：发送单帧图片（base64 data URI）给多模态模型，返回 JSON 字典。

        仅用于必要的关键帧分类（不会发送整段视频）。无客户端时返回 None。
        """
        if not self.available():
            return None
        import base64
        try:
            with open(image_path, "rb") as f:
                b64 = base64.b64encode(f.read()).decode("ascii")
        except Exception as e:  # noqa
            print(f"[warn] 读取帧失败: {e}")
            return None
        data_uri = f"data:image/png;base64,{b64}"
        try:
            resp = self.client.chat.completions.create(
                model=self.cfg.llm_model,
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url",
                         "image_url": {"url": data_uri}},
                    ],
                }],
                temperature=0,
            )
            return _extract_json(resp.choices[0].message.content)
        except Exception as e:  # noqa
            print(f"[warn] 视觉 LLM 分类失败，回退启发式: {e}")
            return None

    def detect_sensitive(self, segments: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """返回 [{'index':int, 'phrase':str}, ...]"""
        if not self.available():
            return []
        text = "\n".join(f"{i}. {s['text']}" for i, s in enumerate(segments))
        prompt = (
            "以下是销售/客户沟通视频的字幕文本。请识别其中涉及的敏感商业信息"
            "（如营业额、销售额、利润、成本、客户名称、联系方式、产品价格、库存数量等）。\n"
            "只返回 JSON 数组，元素形如 {\"index\": 句子编号, \"phrase\": \"敏感短语\"}。"
            "不要返回任何其他内容。\n\n" + text
        )
        try:
            resp = self.client.chat.completions.create(
                model=self.cfg.llm_model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0,
            )
            return _extract_json(resp.choices[0].message.content)
        except Exception as e:  # noqa
            print(f"[warn] LLM 敏感检测失败，回退到关键词匹配: {e}")
            return []

    def cover_title(self, transcript: str) -> str:
        """根据字幕生成短视频风格封面标题（高质量）。

        要求：前 8 字有钩子（痛点/疑问/数字）、含具体行业场景词、落到痛点或收益，
        而非空泛概括。一次生成 3 个候选并由模型选最稳的 1 个；解析失败时按行兜底。
        """
        if not self.available() or not transcript.strip():
            return ""
        prompt = (
            "你是一名资深短视频运营，要为一段软件产品演示视频取封面/标题。\n"
            "视频字幕转写如下：\n"
            f"{transcript[:2000]}\n\n"
            "要求：\n"
            "1. 每个标题不超过 20 字，适合短视频封面与视频号草稿标题；\n"
            "2. 前 8 字必须有钩子：用痛点、疑问或数字抓人（如'对账总乱？''还靠Excel？'"
            "'3000种商品怎么管'）；\n"
            "3. 必须包含具体行业/场景词（如化工批发、进销存、对账、批量导入），不要空泛；\n"
            "4. 落到痛点或收益，不要只做'是什么'的概括；\n"
            "5. 避免品牌名与夸张违规词。\n"
            "请只返回 JSON：{\"titles\":[\"候选1\",\"候选2\",\"候选3\"],\"best\":0}，"
            "best 为最推荐候选的下标（0/1/2）。不要任何解释文字。"
        )
        try:
            resp = self.client.chat.completions.create(
                model=self.cfg.llm_model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.8,
            )
            content = resp.choices[0].message.content.strip()
            data = _extract_json(content)
            if isinstance(data, dict) and data.get("titles"):
                titles = [str(t).strip().strip('"').strip()
                          for t in data["titles"] if str(t).strip()]
                if titles:
                    try:
                        best = int(data.get("best", 0))
                    except Exception:
                        best = 0
                    if 0 <= best < len(titles):
                        return titles[best]
                    return titles[0]
            # 兜底：按非空行解析（模型偶尔直接吐标题/多行）
            lines = [l.strip().strip('"').strip()
                     for l in content.splitlines() if l.strip()]
            if lines:
                return lines[0]
            return ""
        except Exception as e:  # noqa
            print(f"[warn] LLM 封面标题生成失败: {e}")
            return ""
