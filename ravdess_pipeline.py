from __future__ import annotations

from pathlib import Path
from typing import Iterable

import librosa
import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestClassifier
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.neural_network import MLPClassifier
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler
from tqdm.auto import tqdm

EMOTION_MAP = {
    "01": "neutral",
    "02": "calm",
    "03": "happy",
    "04": "sad",
    "05": "angry",
    "06": "fearful",
    "07": "disgust",
    "08": "surprised",
}

INTENSITY_MAP = {"01": "normal", "02": "strong"}
STATEMENT_MAP = {
    "01": "Kids are talking by the door",
    "02": "Dogs are sitting by the door",
}


def _contains_ravdess_audio(directory: Path) -> bool:
    """Return True when a directory appears to contain RAVDESS wav files."""
    return directory.exists() and any(directory.rglob("*.wav"))


def resolve_dataset_dir(dataset_dir: str | Path, download_if_missing: bool = True) -> Path:
    """Resolve the most likely local RAVDESS directory and optionally download it from Kaggle."""
    dataset_dir = Path(dataset_dir)

    candidate_dirs = [
        dataset_dir,
        dataset_dir.parent,
        Path.home() / ".cache" / "kagglehub" / "datasets" / "uwrfkaggler" / "ravdess-emotional-speech-audio" / "versions" / "1",
    ]

    for candidate in candidate_dirs:
        if _contains_ravdess_audio(candidate):
            return candidate

    if download_if_missing:
        try:
            import kagglehub

            downloaded_path = Path(kagglehub.dataset_download("uwrfkaggler/ravdess-emotional-speech-audio"))
            if _contains_ravdess_audio(downloaded_path):
                return downloaded_path
        except Exception:
            pass

    return dataset_dir


def parse_ravdess_filename(file_path: str | Path) -> dict:
    """Parse a RAVDESS file name and return structured metadata."""
    file_path = Path(file_path)
    parts = file_path.stem.split("-")
    if len(parts) != 7:
        raise ValueError(f"Unexpected RAVDESS file name: {file_path.name}")

    modality, vocal_channel, emotion, intensity, statement, repetition, actor = parts
    actor_id = int(actor)

    return {
        "path": str(file_path),
        "file_name": file_path.name,
        "modality": modality,
        "vocal_channel": vocal_channel,
        "emotion_code": emotion,
        "label": EMOTION_MAP.get(emotion, "unknown"),
        "intensity_code": intensity,
        "intensity": INTENSITY_MAP.get(intensity, "unknown"),
        "statement_code": statement,
        "statement_text": STATEMENT_MAP.get(statement, "unknown statement"),
        "repetition": repetition,
        "actor_id": actor_id,
        "gender": "female" if actor_id % 2 == 0 else "male",
    }


def build_metadata(dataset_dir: str | Path) -> pd.DataFrame:
    """Scan the dataset directory recursively and build a metadata table."""
    dataset_dir = resolve_dataset_dir(dataset_dir, download_if_missing=True)
    wav_files = sorted(dataset_dir.rglob("*.wav"))

    rows = []
    for wav_path in wav_files:
        try:
            rows.append(parse_ravdess_filename(wav_path))
        except ValueError:
            continue

    metadata = pd.DataFrame(rows)
    if not metadata.empty:
        metadata["path"] = metadata["path"].astype(str)
    return metadata


def extract_audio_features(
    file_path: str | Path,
    sample_rate: int = 22050,
    duration: float | None = 3.0,
    offset: float = 0.0,
    n_mfcc: int = 13,
) -> np.ndarray:
    """Extract robust summary audio features using librosa."""
    signal, sr = librosa.load(file_path, sr=sample_rate, duration=duration, offset=offset)

    if signal.size == 0:
        raise ValueError(f"Empty audio signal for {file_path}")

    mfcc = librosa.feature.mfcc(y=signal, sr=sr, n_mfcc=n_mfcc)
    mfcc_delta = librosa.feature.delta(mfcc)
    spectral_centroid = librosa.feature.spectral_centroid(y=signal, sr=sr)
    zcr = librosa.feature.zero_crossing_rate(signal)
    chroma = librosa.feature.chroma_stft(y=signal, sr=sr)
    rms = librosa.feature.rms(y=signal)

    feature_vector = np.hstack(
        [
            mfcc.mean(axis=1),
            mfcc.std(axis=1),
            mfcc_delta.mean(axis=1),
            mfcc_delta.std(axis=1),
            [spectral_centroid.mean(), spectral_centroid.std()],
            [zcr.mean(), zcr.std()],
            chroma.mean(axis=1),
            chroma.std(axis=1),
            [rms.mean(), rms.std()],
        ]
    )

    return feature_vector.astype(np.float32)


def get_feature_names(n_mfcc: int = 13) -> list[str]:
    names = []
    names += [f"mfcc_mean_{i + 1}" for i in range(n_mfcc)]
    names += [f"mfcc_std_{i + 1}" for i in range(n_mfcc)]
    names += [f"mfcc_delta_mean_{i + 1}" for i in range(n_mfcc)]
    names += [f"mfcc_delta_std_{i + 1}" for i in range(n_mfcc)]
    names += ["spectral_centroid_mean", "spectral_centroid_std"]
    names += ["zcr_mean", "zcr_std"]
    names += [f"chroma_mean_{i + 1}" for i in range(12)]
    names += [f"chroma_std_{i + 1}" for i in range(12)]
    names += ["rms_mean", "rms_std"]
    return names


def build_feature_matrix(
    file_paths: Iterable[str | Path],
    sample_rate: int = 22050,
    duration: float | None = 3.0,
    offset: float = 0.0,
    n_mfcc: int = 13,
) -> tuple[np.ndarray, list[str]]:
    """Transform a list of audio files into a 2D feature matrix."""
    features = []
    for file_path in tqdm(list(file_paths), desc="Extracting audio features"):
        features.append(
            extract_audio_features(
                file_path=file_path,
                sample_rate=sample_rate,
                duration=duration,
                offset=offset,
                n_mfcc=n_mfcc,
            )
        )

    X = np.vstack(features)
    feature_names = get_feature_names(n_mfcc=n_mfcc)
    return X, feature_names


def get_models(random_state: int = 42) -> dict[str, object]:
    """Return fast baseline models for the classification task."""
    random_forest = RandomForestClassifier(
        n_estimators=500,
        max_features="sqrt",
        class_weight="balanced",
        random_state=random_state,
        n_jobs=-1,
    )

    mlp = Pipeline(
        steps=[
            ("scaler", StandardScaler()),
            (
                "mlp",
                MLPClassifier(
                    hidden_layer_sizes=(256, 128),
                    activation="relu",
                    alpha=1e-4,
                    batch_size=32,
                    learning_rate_init=1e-3,
                    early_stopping=True,
                    validation_fraction=0.15,
                    n_iter_no_change=20,
                    max_iter=400,
                    random_state=random_state,
                ),
            ),
        ]
    )

    return {
        "RandomForest": random_forest,
        "MLP": mlp,
    }


def find_most_confused_pairs(cm: np.ndarray, class_names: list[str], top_k: int = 5) -> list[dict]:
    """Return the most confused off-diagonal class pairs from the confusion matrix."""
    confusions = []
    for i, true_name in enumerate(class_names):
        for j, pred_name in enumerate(class_names):
            if i != j and cm[i, j] > 0:
                confusions.append(
                    {
                        "true_label": true_name,
                        "predicted_label": pred_name,
                        "count": int(cm[i, j]),
                    }
                )

    confusions.sort(key=lambda item: item["count"], reverse=True)
    return confusions[:top_k]


def create_metadata_features(metadata: pd.DataFrame) -> tuple[np.ndarray, TfidfVectorizer]:
    """Create auxiliary metadata features from statement, intensity, and speaker gender."""
    metadata_corpus = (
        metadata["statement_text"].astype(str)
        + " | intensity="
        + metadata["intensity"].astype(str)
        + " | gender="
        + metadata["gender"].astype(str)
    )

    vectorizer = TfidfVectorizer(ngram_range=(1, 2), min_df=1)
    metadata_features = vectorizer.fit_transform(metadata_corpus).toarray().astype(np.float32)
    return metadata_features, vectorizer


def create_fake_text_features(metadata: pd.DataFrame) -> tuple[np.ndarray, TfidfVectorizer]:
    """Backward-compatible alias for the older function name."""
    return create_metadata_features(metadata)
