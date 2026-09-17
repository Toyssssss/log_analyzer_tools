import re
import os
from typing import List, Tuple, Dict


def search_in_mapping(mapping: List[Tuple[str, str]], keyword: str, match_case: bool = False, whole_word: bool = False) -> List[Dict]:
    """
    在 mapping 中搜索 keyword。
    返回匹配项的字典列表：{'virtual_path', 'fs_path', 'line_no', 'line'}
    """
    if not keyword:
        return []

    flags = 0 if match_case else re.IGNORECASE
    pattern = re.escape(keyword)
    if whole_word:
        pattern = r'\b' + pattern + r'\b'
    regex = re.compile(pattern, flags)

    results = []

    for virtual_path, fs_path in mapping:
        if not os.path.isfile(fs_path):
            continue
        try:
            with open(fs_path, 'r', errors='ignore', encoding='utf-8') as f:
                for i, line in enumerate(f, start=1):
                    if regex.search(line):
                        results.append({'virtual_path': virtual_path, 'fs_path': fs_path, 'line_no': i, 'line': line.strip()})
                        break
        except Exception:
            # 无法读取的文件跳过
            continue

    return results
