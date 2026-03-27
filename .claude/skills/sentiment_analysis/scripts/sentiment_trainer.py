"""
Sentiment ML Trainer & Predictor
情感分析 ML 训练器 - 从纯词典匹配升级到 TF-IDF + ML 分类

支持:
- TF-IDF 特征提取 + LogisticRegression / SVM 分类
- 弱标签生成（基于新闻发布后3日价格变动）
- 模型持久化（joblib）
- 中文分词（jieba）
- 无依赖时优雅降级到词典匹配
"""

import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger

# ---------------------------------------------------------------------------
# Optional imports with graceful fallback
# ---------------------------------------------------------------------------
try:
    import jieba
    JIEBA_AVAILABLE = True
except ImportError:
    JIEBA_AVAILABLE = False
    logger.warning("jieba not installed — Chinese tokenization disabled, falling back to char split")

try:
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.linear_model import LogisticRegression
    from sklearn.svm import LinearSVC
    from sklearn.pipeline import Pipeline
    from sklearn.model_selection import cross_val_score
    from sklearn.metrics import classification_report, accuracy_score
    import joblib
    import numpy as np
    SKLEARN_AVAILABLE = True
except ImportError:
    SKLEARN_AVAILABLE = False
    logger.warning("scikit-learn not installed — ML sentiment model disabled, using dictionary fallback")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
MODEL_DIR = Path(__file__).resolve().parent.parent / "models"

# Chinese stop words (compact set covering the most common ones)
CHINESE_STOP_WORDS = [
    "的", "了", "在", "是", "我", "有", "和", "就", "不", "人", "都", "一",
    "一个", "上", "也", "很", "到", "说", "要", "去", "你", "会", "着", "没有",
    "看", "好", "自己", "这", "他", "她", "它", "们", "那", "些", "被", "从",
    "对", "以", "但", "与", "而", "或", "其", "之", "又", "将", "已", "及",
    "把", "让", "向", "等", "能", "可以", "还", "比", "最", "该", "这个",
    "为", "中", "所", "后", "前", "下", "个", "里", "来", "多", "大", "小",
    "因为", "所以", "如果", "虽然", "但是", "然后", "可是", "而且", "因此",
    "并且", "或者", "不过", "只是", "这样", "那样", "什么", "怎么", "哪",
]


def _tokenize_chinese(text: str) -> str:
    """Tokenize Chinese text using jieba; fall back to character splitting."""
    if JIEBA_AVAILABLE:
        return " ".join(jieba.cut(text))
    # Naive fallback: split every character (poor but functional)
    return " ".join(list(text))


# ============================================================================
# SentimentMLTrainer
# ============================================================================

class SentimentMLTrainer:
    """Train a TF-IDF + ML sentiment classifier for financial news."""

    def __init__(self, model_type: str = "logistic_regression"):
        """
        Args:
            model_type: "logistic_regression" (primary) or "svm" (fallback).
        """
        if not SKLEARN_AVAILABLE:
            raise RuntimeError("scikit-learn is required for SentimentMLTrainer")

        self.model_type = model_type
        self.pipeline: Optional[Pipeline] = None
        self._build_pipeline()

    def _build_pipeline(self) -> None:
        """Construct the TF-IDF + classifier pipeline."""
        tfidf = TfidfVectorizer(
            tokenizer=lambda t: t.split(),
            max_features=5000,
            ngram_range=(1, 2),
            stop_words=CHINESE_STOP_WORDS,
            sublinear_tf=True,
        )

        if self.model_type == "svm":
            clf = LinearSVC(max_iter=2000, class_weight="balanced")
        else:
            clf = LogisticRegression(
                max_iter=1000,
                class_weight="balanced",
                solver="lbfgs",
                multi_class="multinomial",
            )

        self.pipeline = Pipeline([
            ("tfidf", tfidf),
            ("clf", clf),
        ])

    # ------------------------------------------------------------------
    # Training
    # ------------------------------------------------------------------

    def train(self, texts: List[str], labels: List[int]) -> Dict[str, Any]:
        """
        Train the ML model.

        Args:
            texts:  Raw text samples (Chinese).
            labels: Integer labels — 0 (negative), 1 (neutral), 2 (positive).

        Returns:
            Dict with training metrics.
        """
        if len(texts) != len(labels):
            raise ValueError("texts and labels must have the same length")
        if len(texts) < 10:
            raise ValueError("Need at least 10 samples for training")

        logger.info("Tokenizing {} samples ...", len(texts))
        tokenized = [_tokenize_chinese(t) for t in texts]

        logger.info("Training {} model ...", self.model_type)
        try:
            self.pipeline.fit(tokenized, labels)
        except Exception as exc:
            if self.model_type != "svm":
                logger.warning("LogisticRegression failed ({}), falling back to SVM", exc)
                self.model_type = "svm"
                self._build_pipeline()
                self.pipeline.fit(tokenized, labels)
            else:
                raise

        # Evaluate with cross-validation when we have enough data
        metrics: Dict[str, Any] = {"model_type": self.model_type, "n_samples": len(texts)}
        if len(texts) >= 30:
            try:
                cv_scores = cross_val_score(self.pipeline, tokenized, labels, cv=min(5, len(texts) // 5), scoring="accuracy")
                metrics["cv_accuracy_mean"] = float(np.mean(cv_scores))
                metrics["cv_accuracy_std"] = float(np.std(cv_scores))
                logger.info("CV accuracy: {:.3f} +/- {:.3f}", metrics["cv_accuracy_mean"], metrics["cv_accuracy_std"])
            except Exception as exc:
                logger.warning("Cross-validation skipped: {}", exc)

        # Refit on full data (cross_val_score does not mutate the estimator)
        self.pipeline.fit(tokenized, labels)

        # In-sample accuracy (informational only)
        preds = self.pipeline.predict(tokenized)
        metrics["train_accuracy"] = float(accuracy_score(labels, preds))
        logger.info("Train accuracy: {:.3f}", metrics["train_accuracy"])

        return metrics

    # ------------------------------------------------------------------
    # Prediction
    # ------------------------------------------------------------------

    def predict(self, text: str) -> Tuple[float, float]:
        """
        Predict sentiment for a single text.

        Returns:
            (sentiment_score, confidence) — both in [0, 1].
            sentiment_score: 0=negative, 0.5=neutral, 1=positive.
        """
        if self.pipeline is None:
            raise RuntimeError("Model has not been trained yet")

        tokenized = _tokenize_chinese(text)
        label = int(self.pipeline.predict([tokenized])[0])

        # Map label to score
        score = label / 2.0  # 0->0.0, 1->0.5, 2->1.0

        # Confidence from decision function or predict_proba
        confidence = self._get_confidence(tokenized)
        return score, confidence

    def predict_batch(self, texts: List[str]) -> List[Tuple[float, float]]:
        """Predict sentiment for multiple texts."""
        if self.pipeline is None:
            raise RuntimeError("Model has not been trained yet")

        tokenized = [_tokenize_chinese(t) for t in texts]
        labels = self.pipeline.predict(tokenized)

        results: List[Tuple[float, float]] = []
        for i, label in enumerate(labels):
            score = int(label) / 2.0
            confidence = self._get_confidence(tokenized[i])
            results.append((score, confidence))
        return results

    def _get_confidence(self, tokenized_text: str) -> float:
        """Extract confidence from the classifier."""
        clf = self.pipeline.named_steps["clf"]
        try:
            if hasattr(clf, "predict_proba"):
                proba = clf.predict_proba([tokenized_text])[0]
                return float(max(proba))
            elif hasattr(clf, "decision_function"):
                dec = clf.decision_function([tokenized_text])[0]
                if hasattr(dec, "__len__"):
                    # Multi-class: use max absolute decision value, map to [0.5, 1]
                    raw = float(max(abs(d) for d in dec))
                else:
                    raw = float(abs(dec))
                # Sigmoid-like mapping to [0.5, 1.0]
                return 0.5 + 0.5 * min(raw / (raw + 1.0), 1.0)
        except Exception:
            pass
        return 0.5

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def save(self, path: str) -> None:
        """Save the trained pipeline to disk using joblib."""
        if self.pipeline is None:
            raise RuntimeError("No model to save")
        save_path = Path(path)
        save_path.parent.mkdir(parents=True, exist_ok=True)
        joblib.dump(self.pipeline, str(save_path))
        logger.info("Model saved to {}", save_path)

    def load(self, path: str) -> None:
        """Load a trained pipeline from disk."""
        load_path = Path(path)
        if not load_path.exists():
            raise FileNotFoundError(f"Model file not found: {load_path}")
        self.pipeline = joblib.load(str(load_path))
        logger.info("Model loaded from {}", load_path)

    # ------------------------------------------------------------------
    # Weak labeling
    # ------------------------------------------------------------------

    @staticmethod
    def generate_weak_labels(
        news_with_prices: List[Dict],
    ) -> Tuple[List[str], List[int]]:
        """
        Generate weak training labels from news items paired with price data.

        Each dict in *news_with_prices* should contain:
            - "text" (str): news headline / content
            - "price_before" (float): closing price on the news date
            - "price_after" (float): closing price 3 trading days later

        Labeling rule (3-day return):
            >  +2%  -> positive (2)
            <  -2%  -> negative (0)
            else    -> neutral  (1)

        Returns:
            (texts, labels)
        """
        texts: List[str] = []
        labels: List[int] = []

        for item in news_with_prices:
            text = item.get("text", "").strip()
            price_before = item.get("price_before")
            price_after = item.get("price_after")

            if not text or price_before is None or price_after is None:
                continue
            if price_before <= 0:
                continue

            change_pct = (price_after - price_before) / price_before

            if change_pct > 0.02:
                label = 2  # positive
            elif change_pct < -0.02:
                label = 0  # negative
            else:
                label = 1  # neutral

            texts.append(text)
            labels.append(label)

        logger.info(
            "Weak labeling complete: {} samples (pos={}, neu={}, neg={})",
            len(labels),
            labels.count(2),
            labels.count(1),
            labels.count(0),
        )
        return texts, labels


# ============================================================================
# SentimentMLPredictor
# ============================================================================

class SentimentMLPredictor:
    """
    Production predictor — loads a pre-trained ML model when available,
    otherwise falls back to dictionary-based matching.
    """

    def __init__(self, model_path: Optional[str] = None):
        self._trainer: Optional[SentimentMLTrainer] = None
        self._model_loaded = False

        if model_path is None:
            model_path = str(MODEL_DIR / "sentiment_model.pkl")

        if SKLEARN_AVAILABLE and Path(model_path).exists():
            try:
                self._trainer = SentimentMLTrainer()
                self._trainer.load(model_path)
                self._model_loaded = True
                logger.info("ML sentiment model loaded successfully")
            except Exception as exc:
                logger.warning("Failed to load ML model: {} — using dictionary fallback", exc)
                self._trainer = None
                self._model_loaded = False
        else:
            if not SKLEARN_AVAILABLE:
                logger.info("sklearn unavailable — using dictionary fallback")
            else:
                logger.info("No pre-trained model at {} — using dictionary fallback", model_path)

    @property
    def is_ml_ready(self) -> bool:
        return self._model_loaded and self._trainer is not None

    def predict(self, text: str) -> Dict[str, Any]:
        """
        Predict sentiment for a single text.

        Returns:
            {"score": float 0-1, "confidence": float 0-1, "method": "ml"|"dictionary"}
        """
        if self.is_ml_ready:
            try:
                score, confidence = self._trainer.predict(text)
                return {
                    "score": round(score, 4),
                    "confidence": round(confidence, 4),
                    "method": "ml",
                }
            except Exception as exc:
                logger.warning("ML prediction failed: {} — falling back to dictionary", exc)

        # Dictionary fallback
        score, confidence = self._dictionary_predict(text)
        return {
            "score": round(score, 4),
            "confidence": round(confidence, 4),
            "method": "dictionary",
        }

    # ------------------------------------------------------------------
    # Dictionary-based fallback
    # ------------------------------------------------------------------

    # Inline word lists to avoid circular imports from sentiment_analysis.py
    _POSITIVE_STRONG = [
        "暴涨", "飙升", "大涨", "涨停", "突破", "创新高", "井喷", "强势",
        "利好", "重大突破", "超预期", "爆发", "领涨", "翻倍", "历史新高",
    ]
    _POSITIVE_MODERATE = [
        "上涨", "走高", "看好", "增长", "回升", "反弹", "企稳", "上行",
        "向好", "改善", "提升", "盈利", "增持", "买入", "乐观", "积极",
    ]
    _NEGATIVE_STRONG = [
        "暴跌", "崩盘", "跌停", "大跌", "重挫", "断崖", "闪崩", "熔断",
        "爆仓", "破产", "退市", "违规", "造假", "欺诈", "暴雷", "崩溃",
    ]
    _NEGATIVE_MODERATE = [
        "下跌", "走低", "看空", "下滑", "回落", "承压", "疲软", "低迷",
        "利空", "减持", "卖出", "悲观", "亏损", "下调", "萎缩", "风险",
    ]

    def _dictionary_predict(self, text: str) -> Tuple[float, float]:
        """Simple dictionary matching as a fallback."""
        score_sum = 0.0
        hit_count = 0

        for word in self._POSITIVE_STRONG:
            if word in text:
                score_sum += 2.0
                hit_count += 1
        for word in self._POSITIVE_MODERATE:
            if word in text:
                score_sum += 1.0
                hit_count += 1
        for word in self._NEGATIVE_STRONG:
            if word in text:
                score_sum -= 2.0
                hit_count += 1
        for word in self._NEGATIVE_MODERATE:
            if word in text:
                score_sum -= 1.0
                hit_count += 1

        if hit_count == 0:
            return 0.5, 0.1  # neutral, low confidence

        # Normalize score to [0, 1]
        # Raw range is roughly [-2*n, +2*n]; use tanh-like mapping
        normalized = score_sum / (abs(score_sum) + 3.0)  # maps to (-1, 1)
        final_score = (normalized + 1.0) / 2.0           # maps to (0, 1)
        confidence = min(hit_count / 5.0, 1.0) * 0.6     # max 0.6 for dictionary

        return final_score, confidence


# ============================================================================
# Module-level singleton
# ============================================================================

_predictor_instance: Optional[SentimentMLPredictor] = None


def get_sentiment_predictor() -> SentimentMLPredictor:
    """Get or create the global SentimentMLPredictor singleton."""
    global _predictor_instance
    if _predictor_instance is None:
        _predictor_instance = SentimentMLPredictor()
    return _predictor_instance
