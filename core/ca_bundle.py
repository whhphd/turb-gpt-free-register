# -*- coding: utf-8 -*-
"""为 curl_cffi / OpenSSL 准备纯 ASCII 路径的 CA 证书包。

Windows 上若项目安装在含中文的路径（如 D:\\同步\\...），certifi 的 cacert.pem
会落在非 ASCII 路径下，curl_cffi 加载时会报：

    curl: (77) error adding trust anchors from locations: CAfile: D:\\同步\\...

解决办法：把 CA 复制到 %LOCALAPPDATA% 等纯英文路径，并设置 SSL_CERT_FILE /
CURL_CA_BUNDLE / REQUESTS_CA_BUNDLE。
"""
from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)

_FIXED_CA_PATH: str | None = None


def _path_has_non_ascii(path: str | Path) -> bool:
    try:
        str(path).encode("ascii")
        return False
    except UnicodeEncodeError:
        return True


def _preferred_ascii_dir() -> Path:
    for key in ("LOCALAPPDATA", "TEMP", "TMP"):
        raw = str(os.environ.get(key) or "").strip()
        if not raw:
            continue
        candidate = Path(raw)
        if candidate.exists() and not _path_has_non_ascii(candidate):
            return candidate / "turb-gpt-free-register"
    # 最后兜底：当前用户目录下的 .cache（通常也是 ASCII）
    home = Path.home()
    if not _path_has_non_ascii(home):
        return home / ".cache" / "turb-gpt-free-register"
    return Path(os.environ.get("TEMP") or ".") / "turb-gpt-free-register"


def ensure_ascii_ca_bundle(*, force: bool = False) -> str:
    """确保环境变量指向一份纯 ASCII 路径的 CA 文件，返回该路径。"""
    global _FIXED_CA_PATH
    if not force and _FIXED_CA_PATH and Path(_FIXED_CA_PATH).is_file():
        _apply_env(_FIXED_CA_PATH)
        return _FIXED_CA_PATH

    try:
        import certifi
        src = Path(certifi.where())
    except Exception as exc:
        logger.warning("[CA] 无法定位 certifi 证书包: %s: %s", type(exc).__name__, exc)
        existing = (
            os.environ.get("SSL_CERT_FILE")
            or os.environ.get("CURL_CA_BUNDLE")
            or os.environ.get("REQUESTS_CA_BUNDLE")
            or ""
        )
        if existing and Path(existing).is_file():
            _FIXED_CA_PATH = existing
            return existing
        raise

    # 源路径本身已是 ASCII 时，直接使用，无需复制。
    if src.is_file() and not _path_has_non_ascii(src):
        _FIXED_CA_PATH = str(src)
        _apply_env(_FIXED_CA_PATH)
        return _FIXED_CA_PATH

    dst_dir = _preferred_ascii_dir()
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / "cacert.pem"

    need_copy = force or (not dst.is_file())
    if not need_copy:
        try:
            need_copy = dst.stat().st_size != src.stat().st_size or dst.stat().st_mtime < src.stat().st_mtime
        except OSError:
            need_copy = True
    if need_copy:
        shutil.copyfile(src, dst)
        logger.info("[CA] 已复制证书包到纯英文路径: %s -> %s", src, dst)
    else:
        logger.debug("[CA] 复用已有纯英文证书包: %s", dst)

    _FIXED_CA_PATH = str(dst)
    _apply_env(_FIXED_CA_PATH)
    return _FIXED_CA_PATH


def _apply_env(path: str) -> None:
    # 覆盖写入，避免继承到错误的旧值。
    os.environ["SSL_CERT_FILE"] = path
    os.environ["CURL_CA_BUNDLE"] = path
    os.environ["REQUESTS_CA_BUNDLE"] = path
    os.environ["CERT_FILE"] = path


def get_ca_bundle_path() -> str:
    """供 Session(verify=...) 使用。"""
    return ensure_ascii_ca_bundle()
