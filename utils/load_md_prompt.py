from pathlib import Path


REQUIRED_RUNTIME_PROMPTS = (
    "grounder_qwen3_bbox.md",
    "grounder_qwen3_coordinates.md",
    "grounder_bbox.md",
    "grounder_coordinates.md",
    "decider_v2.md",
    "planner_oneshot.md",
    "planner_oneshot_harmony.md",
)


def _prompt_root() -> Path:
    # In a PyInstaller one-folder build ``__file__`` resolves below
    # ``_internal/utils``; source runs resolve below ``<repo>/utils``.  Both
    # layouts intentionally place runtime data in the sibling prompts folder.
    return Path(__file__).resolve().parents[1] / "prompts"


def load_prompt(md_name: str) -> str:
    """Load one runtime prompt in source and frozen-client layouts."""

    prompt_file = _prompt_root() / md_name
    try:
        content = prompt_file.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise FileNotFoundError(
            f"MobiAgent runtime prompt is missing: {prompt_file}"
        ) from exc
    content = content.replace("````markdown", "").replace("````", "")
    return content.strip()


def validate_runtime_prompt_assets() -> tuple[str, ...]:
    """Read every required prompt so packaged smoke tests cannot pass falsely."""

    loaded: list[str] = []
    for name in REQUIRED_RUNTIME_PROMPTS:
        if not load_prompt(name):
            raise RuntimeError(f"MobiAgent runtime prompt is empty: {name}")
        loaded.append(name)
    return tuple(loaded)
