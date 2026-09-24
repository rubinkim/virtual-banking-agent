# 원본 JSON의 복사본을 작업용 데이터로 준비합니다.
# 작업용 JSON을 읽고 변경된 데이터를 저장하는 함수를 구현합니다.
# 초기화가 필요할 때만 원본을 다시 복사합니다.

import json
import os
import shutil
import tempfile
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / 'data'
DATA_PATH = DATA_DIR / 'data.json'
INITIAL_PATH = DATA_DIR / 'initial_data.json'


def load_data() -> dict:
    with open(DATA_PATH, 'r', encoding='utf-8') as f:
        return json.load(f)


def save_data(data: dict) -> None:
    """임시 파일에 먼저 쓴 뒤 교체한다. 쓰는 도중 실패해도 기존 파일은 그대로 유지된다."""
    directory = Path(DATA_PATH).parent
    fd, tmp_path = tempfile.mkstemp(dir=directory, suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, DATA_PATH)
    except BaseException:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise


def reset_data() -> None:
    shutil.copyfile(INITIAL_PATH, DATA_PATH)
