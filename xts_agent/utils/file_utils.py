"""File utility functions."""

from __future__ import annotations
import os
import shutil
import xmltodict
from pathlib import Path
from typing import List, Dict, Any, Optional

def ensure_dir(path: str | Path) -> Path:
    """Ensures a directory exists."""
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    return p

def safe_copy(src: str | Path, dst: str | Path) -> None:
    """Safely copies a file or directory."""
    src_path = Path(src)
    dst_path = Path(dst)
    
    if src_path.is_dir():
        shutil.copytree(src_path, dst_path, dirs_exist_ok=True)
    else:
        ensure_dir(dst_path.parent)
        shutil.copy2(src_path, dst_path)

def find_files(directory: str | Path, pattern: str) -> List[Path]:
    """Finds files matching a pattern recursively."""
    return list(Path(directory).rglob(pattern))

def read_xml(file_path: str | Path) -> str:
    """Reads an XML file."""
    with open(file_path, 'r', encoding='utf-8') as f:
        return f.read()

def parse_xml_to_dict(xml_content: str) -> Dict[str, Any]:
    """Parses XML string to dictionary."""
    return xmltodict.parse(xml_content)

def archive_directory(src_dir: str | Path, archive_path: str | Path, format: str = 'zip') -> str:
    """Archives a directory."""
    src_path = Path(src_dir)
    arch_path = Path(archive_path)
    ensure_dir(arch_path.parent)
    
    base_name = str(arch_path.with_suffix(''))
    return shutil.make_archive(base_name, format, src_path)
