"""Application inputs and defaults, independent of command-line parsing."""

import re
from dataclasses import dataclass
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parents[3] / "data"


@dataclass(frozen=True)
class CollectionRequest:
    target: str
    mode: str
    db: str = str(DATA_DIR / "index.sqlite3")
    language: str | None = None
    engine: str | None = None
    engine_model: str | None = None
    device: str = "auto"
    cookies: str | None = None
    refresh: bool = False

    def validate(self) -> None:
        if not self.target.strip():
            raise ValueError("Collection target cannot be empty.")
        if self.mode not in {"direct", "generate"}:
            raise ValueError("Choose direct or generate as collection mode.")
        if self.device not in {"auto", "cpu", "cuda"}:
            raise ValueError("Choose auto, cpu or cuda as device.")
        if self.mode == "direct":
            if self.engine or self.engine_model:
                raise ValueError(
                    "--engine and --engine-model are only valid in generate mode."
                )
            if not self.language or not re.fullmatch(
                r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*", self.language
            ):
                raise ValueError(
                    "Direct mode requires one --language, for example vi or en."
                )
        elif self.language:
            raise ValueError(
                "--language selects YouTube captions and is only valid in direct mode."
            )
