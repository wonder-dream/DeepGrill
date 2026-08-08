import argparse
from pathlib import Path

from . import db

DEFAULT_DB_URL = "data/interview.db"


def main() -> None:
    parser = argparse.ArgumentParser(prog="app")
    sub = parser.add_subparsers(dest="command", required=True)

    imp = sub.add_parser("import", help="导入面经/简历文件为 Source")
    imp.add_argument("file", type=Path)
    imp.add_argument("--type", choices=["manual", "resume"], default="manual")

    args = parser.parse_args()
    if args.command == "import":
        Path(DEFAULT_DB_URL).parent.mkdir(exist_ok=True)
        db.init_db(f"sqlite:///{DEFAULT_DB_URL}")
        from .crawler.importer import import_file

        generator = _resume_generator() if args.type == "resume" else None
        source = import_file(args.file, args.type, project_generator=generator)
        print(f"source #{source.id} ({args.type}): {source.title}")


def _resume_generator():
    """简历导入联动 M8：project 题生成（受 daily.project_limit 节流）。"""
    from .config import load_config, secret_value
    from .llm.llm_client import LLMClient
    from .pipeline.generate import generate_project_questions

    config = load_config(Path("config.yaml"))
    llm = LLMClient(
        config.llm.generate_model,
        config.llm.base_url,
        secret_value(config.llm.api_key_env),
    )

    def generator(source):
        generate_project_questions(source, config.daily.project_limit, llm)

    return generator


if __name__ == "__main__":
    main()
