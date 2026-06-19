#!/usr/bin/env python3
"""
Python implementation of the ETS100 cloud mode used by laststudio/Fuck_ets100.

What it implements:
  - signed ETS100 API requests
  - stable local device_code generation
  - password login and automatic device binding for code=30014
  - ecard account selection and homework list loading
  - CDN ZIP download, ETS100 ZIP password generation, extraction
  - content.json answer parsing for the common structure_type variants

Install optional dependency for encrypted ZIP compatibility:
  pip install pyzipper

Usage examples:
  python ets100_cloud.py login --phone 13800138000 --password your_password
  python ets100_cloud.py list
  python ets100_cloud.py fetch --homework-index 0 --out answers.json
  python ets100_cloud.py parse-local path/to/content.json

This script stores auth/cache data in .ets100_cloud beside the script by default.
Use only with an ETS100 account and content you are authorized to access.
"""

from __future__ import annotations

import argparse
import base64
import dataclasses
import hashlib
import html
import json
import os
import random
import re
import shutil
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from typing import Any, Iterable


API_BASE_URL = "https://api.ets100.com"
CDN_BASE_URL = "https://cdn.subject.ets100.com"
PID = "grlx"
SECRET_KEY = "555ffbe95ccf4e9535a110170b445ab8"
FOOTER_SIZE = 336
TIMEOUT_SECONDS = 30

DEFAULT_SN = "test"
DEFAULT_VERSION = "3"
DEFAULT_REBIND_VERSION = "2"
DEFAULT_SYSTEM = "4"
DEFAULT_GLOBAL_CLIENT_VERSION = "5.4.5"
DEFAULT_DEVICE_NAME = "DESKTOP"
DEFAULT_REBIND_DEVICE_NAME = "1337"
DEFAULT_LOCAL_IP = "127.0.0.1"

STATUS_CURRENT = "1"
STATUS_HISTORY = "2"
STATUS_EXPIRED = "3"


class ETS100Error(RuntimeError):
    pass


class DeviceBindRequired(ETS100Error):
    pass


@dataclasses.dataclass
class EcardAccount:
    key: str
    id: str = ""
    parent_id: str = ""
    user_account_id: str = ""
    name: str = ""
    grade: str = ""
    status: str = ""
    mobile_status: str = ""
    out_of_date: str = ""
    class_id: str = ""
    class_name: str = ""
    machine_code_status: str = ""

    @property
    def is_valid(self) -> bool:
        return (
            self.status == "0"
            and self.out_of_date == "0"
            and bool(self.class_id)
            and self.mobile_status == "1"
        )


@dataclasses.dataclass
class HomeworkContent:
    group_name: str
    url: str


@dataclasses.dataclass
class HomeworkInfo:
    id: str
    name: str
    contents: list[HomeworkContent]


@dataclasses.dataclass
class HomeworkListResponse:
    base_url: str
    homeworks: list[HomeworkInfo]


@dataclasses.dataclass
class Question:
    order: int
    section_order: int
    section_caption: str
    type_name: str
    question_text: str
    answers: list[str]
    original_text: str | None = None
    category: str = ""
    display_order: int | None = None
    options: list[str] | None = None


@dataclasses.dataclass
class Section:
    caption: str
    category: str
    type_name: str
    structure_type: str
    questions: list[Question]
    original_content: str | None = None


@dataclasses.dataclass
class Paper:
    title: str
    sections: list[Section]
    homework_id: str = ""


def compact_json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def md5_hex(data: str | bytes, upper: bool = False) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    digest = hashlib.md5(data).hexdigest()
    return digest.upper() if upper else digest


def md5_middle_16(text: str) -> str:
    return md5_hex(text, upper=False)[8:24]


def normalize_response(raw: bytes) -> dict[str, Any]:
    text = raw.decode("utf-8", errors="replace").strip()
    if not text:
        return {}
    data = json.loads(text)
    if isinstance(data, list):
        return data[0] if data else {}
    if not isinstance(data, dict):
        raise ETS100Error(f"unexpected response type: {type(data).__name__}")
    return data


class StateStore:
    def __init__(self, root: Path):
        self.root = root
        self.auth_path = root / "auth.json"
        self.cache_dir = root / "cloud_homework"
        self.root.mkdir(parents=True, exist_ok=True)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def load(self) -> dict[str, Any]:
        if not self.auth_path.exists():
            return {}
        return json.loads(self.auth_path.read_text(encoding="utf-8"))

    def save(self, data: dict[str, Any]) -> None:
        self.auth_path.write_text(compact_json(data) + "\n", encoding="utf-8")

    def load_or_create_device_code(self) -> str:
        data = self.load()
        if data.get("device_code"):
            return str(data["device_code"])

        data_part = "".join(random.choice("0123456789ABCDEF") for _ in range(16))
        mac_part = "".join(random.choice("0123456789ABCDEF") for _ in range(16))
        device_code = f"{md5_middle_16(data_part)}|{md5_middle_16(mac_part)}"
        data["device_code"] = device_code
        self.save(data)
        return device_code

    def update_auth(self, **values: Any) -> None:
        data = self.load()
        data.update(values)
        self.save(data)


class ETS100Client:
    def __init__(
        self,
        store: StateStore,
        api_base_url: str = API_BASE_URL,
        cdn_base_url: str = CDN_BASE_URL,
        timeout: int = TIMEOUT_SECONDS,
        insecure_cdn: bool = False,
        verbose: bool = False,
    ):
        self.store = store
        self.api_base_url = api_base_url.rstrip("/")
        self.cdn_base_url = cdn_base_url.rstrip("/")
        self.timeout = timeout
        self.insecure_cdn = insecure_cdn
        self.verbose = verbose

    def build_payload(self, route: str, params: dict[str, Any]) -> str:
        timestamp = int(time.time())
        body_data = [{"r": route, "params": params}]
        body_json = compact_json(body_data)
        body_b64 = base64.b64encode(body_json.encode("utf-8")).decode("ascii")
        sign = md5_hex(PID + str(timestamp) + body_b64 + SECRET_KEY)
        return compact_json(
            {
                "body": body_b64,
                "head": {
                    "version": "1.0",
                    "sign": sign,
                    "pid": PID,
                    "time": timestamp,
                },
            }
        )

    def post(self, endpoint: str, route: str, params: dict[str, Any]) -> dict[str, Any]:
        url = self.api_base_url + endpoint
        payload = self.build_payload(route, params).encode("utf-8")
        headers = {
            "Host": "api.ets100.com",
            "User-Agent": "libcurl-agent/1.0",
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "*/*",
        }
        if self.verbose:
            print(f"POST {url}", file=sys.stderr)
        request = urllib.request.Request(url, data=payload, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return normalize_response(response.read())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise ETS100Error(f"HTTP {exc.code}: {detail}") from exc

    def login(self, phone: str, password: str, device_code: str | None = None) -> str:
        device_code = device_code or self.store.load_or_create_device_code()
        response = self.post(
            "/user/login",
            "user/login",
            {
                "sn": DEFAULT_SN,
                "phone": phone,
                "password": password,
                "device_code": device_code,
                "device_name": DEFAULT_DEVICE_NAME,
                "version": DEFAULT_VERSION,
                "local_ip": DEFAULT_LOCAL_IP,
                "system": DEFAULT_SYSTEM,
                "global_client_version": DEFAULT_GLOBAL_CLIENT_VERSION,
                "sign_response": 1,
            },
        )
        code = response.get("code")
        if code not in (None, 0):
            if code == 30014:
                raise DeviceBindRequired(response.get("msg") or "device bind required")
            raise ETS100Error(response.get("msg") or f"login failed, code={code}")

        token = (response.get("body") or {}).get("token") or ""
        if not token:
            raise ETS100Error("login succeeded but token is empty")
        return token

    def bind_device(self, phone: str, password: str, device_code: str | None = None) -> str:
        device_code = device_code or self.store.load_or_create_device_code()
        response = self.post(
            "/user/rebind-code",
            "user/rebind-code",
            {
                "sn": DEFAULT_SN,
                "phone": phone,
                "email": "",
                "password": password,
                "code": "0",
                "version": DEFAULT_REBIND_VERSION,
                "device_name": DEFAULT_REBIND_DEVICE_NAME,
                "device_code": device_code,
                "local_ip": DEFAULT_LOCAL_IP,
                "system": DEFAULT_SYSTEM,
                "global_client_version": DEFAULT_GLOBAL_CLIENT_VERSION,
                "sign_response": 1,
            },
        )
        code = response.get("code")
        if code not in (None, 0):
            raise ETS100Error(response.get("msg") or f"bind failed, code={code}")
        token = (response.get("body") or {}).get("token") or ""
        if not token:
            raise ETS100Error("bind succeeded but token is empty")
        return token

    def login_or_bind(self, phone: str, password: str) -> str:
        device_code = self.store.load_or_create_device_code()
        try:
            return self.login(phone, password, device_code)
        except DeviceBindRequired:
            return self.bind_device(phone, password, device_code)

    def get_ecard_accounts(self, token: str) -> list[EcardAccount]:
        response = self.post(
            "/m/ecard/list",
            "m/ecard/list",
            {
                "sn": DEFAULT_SN,
                "token": token,
                "version": DEFAULT_VERSION,
                "system": DEFAULT_SYSTEM,
                "global_client_version": DEFAULT_GLOBAL_CLIENT_VERSION,
                "sign_response": 1,
            },
        )
        body = response.get("body") or {}
        accounts: list[EcardAccount] = []
        for key, raw in body.items():
            if not isinstance(raw, dict):
                continue
            accounts.append(
                EcardAccount(
                    key=str(key),
                    id=str(raw.get("id") or ""),
                    parent_id=str(raw.get("parent_id") or ""),
                    user_account_id=str(raw.get("user_account_id") or ""),
                    name=str(raw.get("name") or ""),
                    grade=str(raw.get("grade") or ""),
                    status=str(raw.get("status") or ""),
                    mobile_status=str(raw.get("mobile_status") or ""),
                    out_of_date=str(raw.get("out_of_date") or ""),
                    class_id=str(raw.get("class_id") or ""),
                    class_name=str(raw.get("class_name") or ""),
                    machine_code_status=str(raw.get("machine_code_status") or ""),
                )
            )
        if not accounts:
            raise ETS100Error("no ecard account found")
        return accounts

    def select_ecard_account(
        self, accounts: list[EcardAccount], preferred_account_id: str | None = None
    ) -> EcardAccount:
        if preferred_account_id:
            for account in accounts:
                if account.id == preferred_account_id and account.is_valid:
                    return account
        for account in accounts:
            if account.is_valid:
                return account
        return accounts[0]

    def get_homework_list(
        self, token: str, parent_account_id: str, status: str = STATUS_CURRENT
    ) -> HomeworkListResponse:
        response = self.post(
            "/g/homework/list",
            "g/homework/list",
            {
                "sn": DEFAULT_SN,
                "token": token,
                "parent_account_id": parent_account_id,
                "limit": "0",
                "status": status,
                "offset": "0",
                "max_end_time": "",
                "max_homework_id": "",
                "min_end_time": "",
                "min_homework_id": "",
                "get_to_do_count": 1,
                "show_old_homework": 1,
                "parent_homework_id": "",
                "get_all_count": 1,
                "check_pass": 1,
                "get_to_overtime_count": 1,
                "version": DEFAULT_VERSION,
                "system": DEFAULT_SYSTEM,
                "global_client_version": DEFAULT_GLOBAL_CLIENT_VERSION,
                "sign_response": 1,
            },
        )
        code = response.get("code")
        if code not in (None, 0):
            raise ETS100Error(response.get("msg") or f"homework list failed, code={code}")
        body = response.get("body") or {}
        base_url = body.get("base_url") or self.cdn_base_url
        homeworks: list[HomeworkInfo] = []
        for item in body.get("data") or []:
            if not isinstance(item, dict):
                continue
            contents: list[HomeworkContent] = []
            for content in ((item.get("struct") or {}).get("contents") or []):
                if isinstance(content, dict) and content.get("url"):
                    contents.append(
                        HomeworkContent(
                            group_name=str(content.get("group_name") or ""),
                            url=str(content.get("url") or ""),
                        )
                    )
            homeworks.append(
                HomeworkInfo(
                    id=str(item.get("id") or ""),
                    name=str(item.get("name") or "unknown homework"),
                    contents=contents,
                )
            )
        return HomeworkListResponse(base_url=base_url, homeworks=homeworks)

    def build_zip_url(self, base_url: str, content_url: str) -> str:
        if content_url.startswith(("http://", "https://")):
            return content_url.replace("http://", "https://", 1)
        base = (base_url or self.cdn_base_url).replace("http://", "https://", 1).rstrip("/")
        path = content_url if content_url.startswith("/") else "/" + content_url
        return base + path

    def download_zip(self, url: str, dest: Path) -> Path:
        if dest.exists() and dest.stat().st_size > 0:
            return dest
        dest.parent.mkdir(parents=True, exist_ok=True)
        headers = {
            "Host": "cdn.subject.ets100.com" if "cdn.subject.ets100.com" in url else "api.ets100.com",
            "User-Agent": "libcurl-agent/1.0",
            "Accept": "*/*",
        }
        context = ssl._create_unverified_context() if self.insecure_cdn else None
        request = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout, context=context) as response:
                dest.write_bytes(response.read())
        except urllib.error.URLError as exc:
            if not self.insecure_cdn and isinstance(getattr(exc, "reason", None), ssl.SSLError):
                raise ETS100Error(
                    "CDN SSL verification failed; rerun with --insecure-cdn if you accept the risk"
                ) from exc
            raise
        check_zip_magic(dest)
        return dest


def check_zip_magic(path: Path) -> None:
    magic = path.read_bytes()[:4]
    if magic not in (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"):
        preview = path.read_bytes()[:120].decode("utf-8", errors="replace")
        raise ETS100Error(f"downloaded file is not a ZIP: {path.name}; preview={preview!r}")


def generate_zip_password(zip_bytes: bytes) -> str:
    if len(zip_bytes) < FOOTER_SIZE:
        raise ETS100Error("ZIP is too small to contain ETS100 footer")
    footer = zip_bytes[-FOOTER_SIZE:]
    if footer[0:8] != b"MSTCHINA" and footer[144:149] != b"EPLAT":
        raise ETS100Error("ETS100 ZIP footer signature not found")
    seed = footer[16:144]
    first_hex = md5_hex(seed, upper=True)
    second_hex = md5_hex(first_hex.encode("ascii"), upper=True)
    return first_hex + second_hex


def safe_extract_zip(zip_path: Path, target_dir: Path, password: str) -> Path:
    if target_dir.exists() and any(target_dir.iterdir()):
        return target_dir
    if target_dir.exists():
        shutil.rmtree(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)

    password_bytes = password.encode("ascii")
    try:
        import pyzipper  # type: ignore

        with pyzipper.AESZipFile(zip_path) as zf:
            zf.pwd = password_bytes
            zf.extractall(target_dir)
        return target_dir
    except ImportError:
        pass
    except Exception:
        shutil.rmtree(target_dir, ignore_errors=True)
        target_dir.mkdir(parents=True, exist_ok=True)

    try:
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(target_dir, pwd=password_bytes)
    except NotImplementedError as exc:
        raise ETS100Error("ZIP encryption is unsupported by stdlib; install pyzipper") from exc
    except RuntimeError as exc:
        raise ETS100Error("ZIP extraction failed; install pyzipper or clear cache and retry") from exc
    return target_dir


def find_content_json(extract_dir: Path) -> Path:
    direct = extract_dir / "content.json"
    if direct.exists():
        return direct
    matches = list(extract_dir.rglob("content.json"))
    if not matches:
        raise ETS100Error(f"content.json not found in {extract_dir}")
    return matches[0]


def clean_display_text(text: Any, split_pipes: bool = False) -> str:
    value = "" if text is None else str(text)
    if split_pipes:
        value = value.replace("|", "\n")
    value = re.sub(r"ets_th\d+\s*", "", value, flags=re.IGNORECASE)
    value = re.sub(r"</p>\s*<p[^>]*>", "\n", value, flags=re.IGNORECASE)
    value = re.sub(r"<br\s*/?>|</br>|</p>|<p[^>]*>", "\n", value, flags=re.IGNORECASE)
    value = re.sub(r"<[^>]+>", "", value)
    value = value.replace("\u200b", "")
    value = html.unescape(value)
    value = value.translate(str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"'}))
    lines = [line.strip() for line in value.splitlines()]
    return "\n".join(line for line in lines if line)


def strip_ask_prefix(text: str) -> str:
    text = text.strip()
    match = re.match(r"^(?:\(?\d+\)?[\s.\u3001\uff0e)\uff09]+)(.+)$", text)
    return match.group(1).strip() if match else text


def as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def std_values(items: Iterable[Any]) -> list[str]:
    values: list[str] = []
    for item in items:
        if isinstance(item, dict):
            values.append(clean_display_text(item.get("value"), split_pipes=True))
        else:
            values.append(clean_display_text(item, split_pipes=True))
    return [value for value in values if value] or ["暂无标准答案"]


def parse_positive_int(value: Any) -> int | None:
    try:
        number = int(str(value).strip())
    except ValueError:
        return None
    return number if number > 0 else None


def extract_display_order(item: dict[str, Any], *keys: str) -> int | None:
    for key in keys:
        text = str(item.get(key) or "")
        match = re.search(r"ets_th\s*(\d+)", text, flags=re.IGNORECASE)
        if match:
            return parse_positive_int(match.group(1))
    for key in ("xt_xh", "xth", "xh", "th", "question_no", "questionNo", "order", "sort", "index"):
        number = parse_positive_int(item.get(key))
        if number is not None:
            return number
    for key in keys:
        text = str(item.get(key) or "")
        match = re.search(r"^\s*(\d+)\s*[.\u3001\uff0e)\uff09]", text)
        if match:
            return parse_positive_int(match.group(1))
    return None


def sort_questions(questions: list[Question]) -> list[Question]:
    return sorted(questions, key=lambda q: (q.display_order or q.section_order, q.section_order))


def parse_content_json(
    data: dict[str, Any],
    type_name: str = "",
    section_caption: str | None = None,
    start_index: int = 0,
) -> Section:
    info = data.get("info") if isinstance(data.get("info"), dict) else {}
    structure_type = str(data.get("structure_type") or "")
    caption = section_caption or type_name or structure_type or "unknown"
    type_label = type_name or caption
    original_content = clean_display_text(info.get("value")) if info.get("value") else None
    questions: list[Question] = []

    def add_question(
        section_order: int,
        question_text: str,
        answers: list[str],
        original_text: str | None = None,
        display_order: int | None = None,
        options: list[str] | None = None,
    ) -> None:
        questions.append(
            Question(
                order=start_index + len(questions) + 1,
                section_order=section_order,
                section_caption=caption,
                type_name=type_label,
                question_text=clean_display_text(question_text),
                answers=[clean_display_text(answer, split_pipes=True) for answer in answers if answer],
                original_text=original_text,
                category=structure_type,
                display_order=display_order,
                options=options,
            )
        )

    if structure_type == "collector.read":
        add_question(1, "模仿朗读原文", [], original_content)

    elif structure_type in ("collector.role", "collector.3q5a", "collector.dialogue", ""):
        items = as_list(info.get("question")) or as_list(info.get("questions"))
        for index, item in enumerate(items, start=1):
            if not isinstance(item, dict):
                continue
            ask = strip_ask_prefix(str(item.get("ask") or ""))
            question_text = ask or str(item.get("question") or item.get("text") or "")
            answers = std_values(as_list(item.get("std")))
            add_question(
                index,
                question_text,
                answers,
                original_content if structure_type != "collector.dialogue" else original_content,
                extract_display_order(item, "ask", "question", "text"),
            )
        if not questions and original_content:
            add_question(1, "阅读理解原文", [original_content], original_content)

    elif structure_type == "collector.picture":
        topic_title = str(info.get("topic") or "信息转述")
        add_question(
            1,
            topic_title,
            std_values(as_list(info.get("std"))),
            original_content,
            extract_display_order(info, "topic", "value"),
        )

    elif structure_type == "collector.choose":
        for index, item in enumerate(as_list(info.get("xtlist")), start=1):
            if not isinstance(item, dict):
                continue
            options: list[str] = []
            for option in as_list(item.get("xxlist")):
                if not isinstance(option, dict):
                    continue
                label = str(option.get("xx_mc") or "")
                content = clean_display_text(option.get("xx_nr"))
                options.append(f"{label}. {content}".strip())
            answer = str(item.get("answer") or "")
            answer_text = "\n".join(options + [f"正确答案: {answer}"]) if options else f"正确答案: {answer}"
            add_question(
                index,
                str(item.get("xt_nr") or item.get("xt_value") or item.get("xt_wj") or ""),
                [answer_text],
                original_content,
                extract_display_order(item, "xt_nr", "xt_value", "xt_wj"),
                options,
            )

    elif structure_type == "collector.fill":
        for index, item in enumerate(as_list(info.get("std")), start=1):
            if not isinstance(item, dict):
                continue
            number = str(item.get("xth") or index)
            add_question(
                index,
                f"第{number}题",
                [clean_display_text(item.get("value"))],
                None,
                extract_display_order(item, "xth", "value"),
            )

    else:
        items = as_list(info.get("question")) or as_list(info.get("questions"))
        if items:
            for index, item in enumerate(items, start=1):
                if not isinstance(item, dict):
                    continue
                add_question(
                    index,
                    str(item.get("ask") or item.get("question") or item.get("text") or ""),
                    std_values(as_list(item.get("std"))),
                    original_content,
                    extract_display_order(item, "ask", "question", "text"),
                )
        elif as_list(info.get("std")):
            add_question(1, str(info.get("topic") or caption), std_values(as_list(info.get("std"))), original_content)

    return Section(
        caption=caption,
        category=structure_type,
        type_name=type_label,
        structure_type=structure_type,
        questions=sort_questions(questions),
        original_content=original_content,
    )


def content_to_section(content_json_path: Path, group_name: str, start_index: int = 0) -> Section:
    data = json.loads(content_json_path.read_text(encoding="utf-8"))
    return parse_content_json(data, type_name=group_name, section_caption=group_name, start_index=start_index)


def sanitize_file_name(name: str) -> str:
    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", name).strip(" .")
    return safe or "homework"


def fetch_homework(client: ETS100Client, response: HomeworkListResponse, homework: HomeworkInfo) -> Paper:
    sections: list[Section] = []
    question_index = 0
    homework_dir_name = sanitize_file_name(homework.id or homework.name)
    for index, content in enumerate(homework.contents):
        zip_url = client.build_zip_url(response.base_url, content.url)
        parsed = urllib.parse.urlparse(zip_url)
        zip_name = Path(parsed.path).name or f"{homework_dir_name}_{index}.zip"
        stem = Path(zip_name).stem or f"content_{index}"
        zip_path = client.store.cache_dir / homework_dir_name / zip_name
        extract_dir = client.store.cache_dir / homework_dir_name / stem
        client.download_zip(zip_url, zip_path)
        password = generate_zip_password(zip_path.read_bytes())
        safe_extract_zip(zip_path, extract_dir, password)
        content_json = find_content_json(extract_dir)
        section = content_to_section(content_json, content.group_name or stem, question_index)
        question_index += len(section.questions)
        sections.append(section)
    return Paper(title=homework.name, sections=sections, homework_id=homework.id)


def dataclass_to_dict(value: Any) -> Any:
    if dataclasses.is_dataclass(value):
        return dataclasses.asdict(value)
    return value


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def first_non_empty(*values: Any) -> str:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def first_answer(question: dict[str, Any]) -> str:
    answers = question.get("answers")
    if isinstance(answers, list) and answers:
        return first_non_empty(answers[0])
    return ""


IPA_TO_WORD = {
    "əˈnæləsɪs": "analysis",
    "ˈrɔɪəl": "royal",
    "ˌɪnɪˈfɪʃnt": "inefficient",
    "ˈjuːnɪvɜːs": "universe",
    "ˈprɪmətɪv": "primitive",
    "əˈbændən": "abandon",
    "əˈsʌmpʃn": "assumption",
    "ˈwɪzdəm": "wisdom",
    "əʊ": "owe",
    "ˈʃædəʊ": "shadow",
    "kənˈvenʃənl": "conventional",
    "ˌsɪvɪlaɪˈzeɪʃən": "civilization",
    "ˌbenɪˈfɪʃl": "beneficial",
    "ˈeɪprən": "apron",
    "ɪnˈtaɪtl": "entitle",
    "ruːˈtiːn": "routine",
    "priˈɒkjupaɪd": "preoccupied",
    "fəˈsɪləti": "facility",
    "fəˈlɒsəfi": "philosophy",
    "rɪˈstrɪkt": "restrict",
    "ˈmaɪkrəʊblɒɡɪŋ": "microblogging",
    "ˈbrɔːdkɑːst": "broadcast",
    "ˌekəˈnɒmɪk": "economic",
    "pəˈlɪtɪkl": "political",
    "daɪˈvɜːs": "diverse",
    "ɪˈlekʃn": "election",
    "kæmˈpeɪn": "campaign",
    "ˈkændɪdeɪt": "candidate",
    "ˈkʌvərɪdʒ": "coverage",
    "əˈveɪləbl": "available",
}


def normalize_ipa(value: str) -> str:
    return re.sub(r"\s+", "", value.strip())


def words_to_rows(words: list[str], row_size: int = 5) -> list[str]:
    return ["    ".join(words[index : index + row_size]) for index in range(0, len(words), row_size)]


def clean_word_reading_answer(text: str) -> str:
    ipa_words: list[str] = []
    for token in re.findall(r"\[([^\]\n]+)\]", text):
        ipa = normalize_ipa(token)
        ipa_words.append(IPA_TO_WORD.get(ipa, f"[{token.strip()}]"))

    without_phonetics = re.sub(r"\[[^\]\n]+\]", " ", text)
    without_phonetics = without_phonetics.replace("\u00a0", " ").replace("聽", " ")
    lines = []
    for line in without_phonetics.splitlines():
        cleaned = re.sub(r"\s+", " ", line).strip()
        if cleaned:
            lines.append(cleaned)

    return "\n".join(words_to_rows(ipa_words) + lines)


def is_word_reading(section_type: str) -> bool:
    return "单词朗读" in section_type


def is_inner_passage_reading(section_type: str) -> bool:
    return "课内语篇朗读" in section_type


def is_story_retelling(section_type: str) -> bool:
    return "故事复述" in section_type


def is_role_play(section_type: str) -> bool:
    return "角色扮演" in section_type


def is_original_text_reading(section: dict[str, Any]) -> bool:
    return section.get("structure_type") == "collector.read"


def is_listen_then_read(section: dict[str, Any], section_type: str) -> bool:
    return section.get("structure_type") == "collector.repeat" and "语篇听读" in section_type


def looks_like_listening_script(text: str) -> bool:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return False
    speaker_lines = sum(1 for line in lines[:8] if re.match(r"^[A-Z]:\s+", line))
    return speaker_lines >= 2 or bool(re.match(r"^[A-Z]:\s+", lines[0]))


def split_title_answer(text: str) -> tuple[str, str]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return "", ""
    if len(lines) == 1:
        return "", lines[0]
    return lines[0], "\n".join(lines[1:])


def build_image_answer_items(paper: dict[str, Any]) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    for section in paper.get("sections") or []:
        if not isinstance(section, dict):
            continue
        section_type = first_non_empty(section.get("caption"), section.get("type_name"), "未知题型")
        questions = [q for q in (section.get("questions") or []) if isinstance(q, dict)]

        if is_word_reading(section_type):
            source = ""
            if questions:
                source = first_non_empty(questions[0].get("original_text"))
            source = first_non_empty(source, section.get("original_content"))
            answer = clean_word_reading_answer(source)
            if answer:
                items.append({"type": section_type, "question": "", "answer": answer})
            continue

        if is_inner_passage_reading(section_type):
            source = ""
            if questions:
                source = first_non_empty(questions[0].get("original_text"))
            answer = first_non_empty(source, section.get("original_content"))
            if answer:
                items.append({"type": section_type, "question": "", "answer": answer})
            continue

        if is_role_play(section_type) and questions:
            role_lines: list[str] = []
            for index, question in enumerate(questions, start=1):
                question_text = first_non_empty(question.get("question_text"))
                answer = first_answer(question)
                if role_lines:
                    role_lines.append("")
                if question_text:
                    role_lines.append(f"{index}. {question_text}")
                elif answer:
                    role_lines.append(f"{index}.")
                if answer:
                    role_lines.append(answer)
            if role_lines:
                items.append({"type": section_type, "question": "", "answer": "\n".join(role_lines)})
            continue

        if is_listen_then_read(section, section_type):
            source = first_non_empty(section.get("original_content"))
            if source and not looks_like_listening_script(source):
                question, answer = split_title_answer(source)
                if answer or question:
                    items.append({"type": section_type, "question": question, "answer": answer})
            continue

        if is_original_text_reading(section):
            source = ""
            if questions:
                source = first_non_empty(questions[0].get("original_text"))
            answer = first_non_empty(source, section.get("original_content"))
            if answer:
                items.append({"type": section_type, "question": "", "answer": answer})
            continue

        if questions:
            for question in questions:
                question_text = first_non_empty(question.get("question_text"))
                answer = first_non_empty(first_answer(question), question.get("original_text"))
                if is_story_retelling(section_type):
                    answer = first_answer(question)
                if answer or question_text:
                    items.append(
                        {
                            "type": section_type,
                            "question": question_text,
                            "answer": answer,
                        }
                    )
            continue

        answer = first_non_empty(section.get("original_content"))
        if answer:
            items.append({"type": section_type, "question": "", "answer": answer})
    return items


class FontStack:
    def __init__(self, primary: Any, fallback: Any):
        self.primary = primary
        self.fallback = fallback

    def iter_runs(self, text: str):
        current_font = None
        current = ""
        for char in text:
            font = self.fallback if needs_cjk_font(char) else self.primary
            if current and font is not current_font:
                yield current_font, current
                current = ""
            current_font = font
            current += char
        if current:
            yield current_font, current

    def getbbox(self, text: str):
        width = 0
        top = 0
        bottom = 0
        for font, run in self.iter_runs(text):
            left, run_top, right, run_bottom = font.getbbox(run)
            width += right - left
            top = min(top, run_top)
            bottom = max(bottom, run_bottom)
        return (0, top, width, bottom)


def needs_cjk_font(char: str) -> bool:
    code = ord(char)
    return (
        0x2E80 <= code <= 0x9FFF
        or 0xF900 <= code <= 0xFAFF
        or 0x3000 <= code <= 0x303F
        or 0xFF00 <= code <= 0xFFEF
    )


def first_existing_path(paths: Iterable[str | Path | None]) -> str | None:
    for path in paths:
        if not path:
            continue
        font_path = Path(path)
        if font_path.exists():
            return str(font_path)
    return None


def load_truetype_font(font_path: str, size: int, bold: bool):
    from PIL import ImageFont

    font = ImageFont.truetype(font_path, size=size)
    if bold and hasattr(font, "get_variation_axes") and hasattr(font, "set_variation_by_axes"):
        try:
            axes = font.get_variation_axes()
            values = []
            for axis in axes:
                name = axis.get("name", b"")
                if isinstance(name, bytes):
                    name = name.decode("ascii", errors="ignore")
                minimum = axis.get("minimum", axis.get("min", 0))
                maximum = axis.get("maximum", axis.get("max", 1000))
                default = axis.get("default", axis.get("def", minimum))
                if "Weight" in str(name) or "wght" in str(name).lower():
                    values.append(min(maximum, max(minimum, 700)))
                else:
                    values.append(default)
            if values:
                font.set_variation_by_axes(values)
        except Exception:
            pass
    return font


def load_font(size: int, bold: bool = False):
    try:
        from PIL import ImageFont
    except ImportError as exc:
        raise ETS100Error("image output requires Pillow; run: python -m pip install -r requirements.txt") from exc

    local_fonts = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "Windows" / "Fonts"
    google_font = first_existing_path(
        [
            local_fonts / "GoogleSans-VariableFont_GRAD,opsz,wght.ttf",
            local_fonts / "GoogleSans-Regular.ttf",
            "C:/Windows/Fonts/GoogleSans-VariableFont_GRAD,opsz,wght.ttf",
            "C:/Windows/Fonts/GoogleSans-Regular.ttf",
        ]
    )
    cjk_font = first_existing_path(
        [
            "C:/Windows/Fonts/msyhbd.ttc" if bold else "C:/Windows/Fonts/msyh.ttc",
            "C:/Windows/Fonts/msyh.ttc",
            "C:/Windows/Fonts/NotoSansSC-VF.ttf",
            "C:/Windows/Fonts/simhei.ttf",
            "C:/Windows/Fonts/Dengb.ttf" if bold else "C:/Windows/Fonts/Deng.ttf",
            "C:/Windows/Fonts/simsun.ttc",
        ]
    )

    if google_font and cjk_font:
        return FontStack(load_truetype_font(google_font, size, bold), load_truetype_font(cjk_font, size, bold))

    font_candidates = [
        google_font,
        "C:/Windows/Fonts/msyhbd.ttc" if bold else "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/NotoSansSC-VF.ttf",
        "C:/Windows/Fonts/simhei.ttf",
        "C:/Windows/Fonts/Dengb.ttf" if bold else "C:/Windows/Fonts/Deng.ttf",
        "C:/Windows/Fonts/simsun.ttc",
    ]
    font_path = first_existing_path(font_candidates)
    if font_path:
        return load_truetype_font(font_path, size, bold)
    return ImageFont.load_default()


def text_width(draw: Any, text: str, font: Any) -> float:
    if isinstance(font, FontStack):
        return sum(text_width(draw, run, run_font) for run_font, run in font.iter_runs(text))
    try:
        return draw.textlength(text, font=font)
    except Exception:
        left, _, right, _ = draw.textbbox((0, 0), text, font=font)
        return right - left


def draw_text(draw: Any, xy: tuple[float, float], text: str, font: Any, fill: str) -> None:
    x, y = xy
    if isinstance(font, FontStack):
        for run_font, run in font.iter_runs(text):
            draw.text((x, y), run, font=run_font, fill=fill)
            x += text_width(draw, run, run_font)
        return
    draw.text((x, y), text, font=font, fill=fill)


def line_height(font: Any, line_gap: int) -> int:
    if isinstance(font, FontStack):
        heights = []
        for item in (font.primary, font.fallback):
            top, bottom = item.getbbox("Hg测")[1], item.getbbox("Hg测")[3]
            heights.append(bottom - top)
        return max(heights) + line_gap
    bbox = font.getbbox("Hg")
    return bbox[3] - bbox[1] + line_gap


def wrap_text(draw: Any, text: str, font: Any, max_width: int) -> list[str]:
    wrapped: list[str] = []

    def append_char_wrapped(value: str) -> None:
        current = ""
        for char in value:
            candidate = current + char
            if current and text_width(draw, candidate, font) > max_width:
                wrapped.append(current.rstrip())
                current = char.lstrip()
            else:
                current = candidate
        if current:
            wrapped.append(current.rstrip())

    for paragraph in str(text).splitlines() or [""]:
        paragraph = paragraph.strip()
        if not paragraph:
            wrapped.append("")
            continue

        if re.search(r"\s", paragraph):
            current = ""
            for token in re.findall(r"\S+\s*", paragraph):
                candidate = current + token
                if current and text_width(draw, candidate, font) > max_width:
                    wrapped.append(current.rstrip())
                    current = ""
                if text_width(draw, token, font) > max_width:
                    if current:
                        wrapped.append(current.rstrip())
                        current = ""
                    append_char_wrapped(token.strip())
                else:
                    current += token
            if current:
                wrapped.append(current.rstrip())
            continue

        current = ""
        for char in paragraph:
            candidate = current + char
            if current and text_width(draw, candidate, font) > max_width:
                wrapped.append(current.rstrip())
                current = char.lstrip()
            else:
                current = candidate
        if current:
            wrapped.append(current.rstrip())
    return wrapped


def draw_wrapped_text(
    draw: Any,
    xy: tuple[int, int],
    text: str,
    font: Any,
    fill: str,
    max_width: int,
    line_gap: int,
) -> int:
    x, y = xy
    item_line_height = line_height(font, line_gap)
    for line in wrap_text(draw, text, font, max_width):
        if line:
            draw_text(draw, (x, y), line, font=font, fill=fill)
        y += item_line_height
    return y


def render_paper_image(paper: dict[str, Any], output_path: Path, width: int = 1600) -> Path:
    try:
        from PIL import Image, ImageDraw
    except ImportError as exc:
        raise ETS100Error("image output requires Pillow; run: python -m pip install -r requirements.txt") from exc

    title_font = load_font(46, bold=True)
    heading_font = load_font(32, bold=True)
    body_font = load_font(30, bold=True)
    label_font = load_font(30, bold=True)
    footer_font = load_font(26, bold=True)
    footer_text = "ETS100_Fucker 目前处于内测阶段，输出的答案可能不准确"
    margin = 72
    content_width = width - margin * 2
    row_gap = 16
    block_gap = 30
    footer_gap = 24
    footer_bottom_margin = 48

    scratch = Image.new("RGB", (width, 100), "#ffffff")
    draw = ImageDraw.Draw(scratch)
    items = build_image_answer_items(paper)

    y = margin
    y = draw_wrapped_text(draw, (margin, y), str(paper.get("title", "")), title_font, "#111827", content_width, 16)
    y += 36

    measurements: list[tuple[dict[str, str], int]] = []
    for item in items:
        start_y = y
        y = draw_wrapped_text(draw, (margin, y), item["type"], label_font, "#111827", content_width, 10)
        if item.get("question"):
            y += row_gap
            y = draw_wrapped_text(draw, (margin, y), item["question"], body_font, "#1f2937", content_width, 10)
        y += row_gap
        y = draw_wrapped_text(draw, (margin, y), item.get("answer", ""), body_font, "#111827", content_width, 10)
        y += block_gap
        measurements.append((item, y - start_y))

    footer_bbox = footer_font.getbbox("Hg")
    footer_height = footer_bbox[3] - footer_bbox[1]
    height = max(y + footer_gap + footer_height + footer_bottom_margin, 900)
    image = Image.new("RGB", (width, height), "#f8fafc")
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((32, 32, width - 32, height - 32), radius=24, fill="#ffffff", outline="#e5e7eb", width=2)

    y = margin
    y = draw_wrapped_text(draw, (margin, y), str(paper.get("title", "")), title_font, "#111827", content_width, 16)
    y += 36

    for item_index, (item, _) in enumerate(measurements):
        block_top = y - 8
        y = draw_wrapped_text(draw, (margin, y), item["type"], label_font, "#111827", content_width, 10)
        if item.get("question"):
            y += row_gap
            y = draw_wrapped_text(draw, (margin, y), item["question"], body_font, "#1f2937", content_width, 10)
        y += row_gap
        y = draw_wrapped_text(draw, (margin, y), item.get("answer", ""), body_font, "#111827", content_width, 10)
        y += block_gap
        if item_index < len(measurements) - 1:
            draw.line((margin, y - 14, width - margin, y - 14), fill="#e5e7eb", width=2)
        if y - block_top > 0:
            draw.rounded_rectangle((margin - 22, block_top, width - margin + 22, y - 22), radius=14, outline="#f3f4f6", width=1)

    footer_width = text_width(draw, footer_text, footer_font)
    footer_x = int((width - footer_width) / 2)
    footer_y = height - footer_bottom_margin - footer_height
    draw_text(draw, (footer_x, footer_y), footer_text, font=footer_font, fill="#9ca3af")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(output_path)
    return output_path


def render_papers_to_images(papers: list[dict[str, Any]], image_dir: Path) -> list[Path]:
    image_dir.mkdir(parents=True, exist_ok=True)
    outputs: list[Path] = []
    used_names: set[str] = set()
    for index, paper in enumerate(papers, start=1):
        base_name = sanitize_file_name(first_non_empty(paper.get("title"), f"paper_{index}"))
        file_name = f"{base_name}.png"
        if file_name.lower() in used_names:
            file_name = f"{index:02d}_{base_name}.png"
        used_names.add(file_name.lower())
        outputs.append(render_paper_image(paper, image_dir / file_name))
    return outputs


def default_root() -> Path:
    return Path(__file__).resolve().parent / ".ets100_cloud"


def make_client(args: argparse.Namespace) -> ETS100Client:
    root = Path(args.root).expanduser().resolve() if args.root else default_root()
    return ETS100Client(
        StateStore(root),
        insecure_cdn=getattr(args, "insecure_cdn", False),
        verbose=getattr(args, "verbose", False),
    )


def ensure_auth(client: ETS100Client) -> tuple[str, str]:
    auth = client.store.load()
    token = auth.get("token")
    parent_id = auth.get("parent_account_id")
    if not token or not parent_id:
        raise ETS100Error("not logged in; run the login command first")
    return str(token), str(parent_id)


def command_login(args: argparse.Namespace) -> None:
    client = make_client(args)
    token = client.login_or_bind(args.phone, args.password)
    accounts = client.get_ecard_accounts(token)
    account = client.select_ecard_account(accounts, args.ecard_id)
    client.store.update_auth(
        phone=args.phone,
        password=args.password if args.save_password else "",
        token=token,
        parent_account_id=account.parent_id,
        selected_ecard_id=account.id,
        selected_ecard_name=account.name,
        selected_ecard_grade=account.grade,
        selected_ecard_class_id=account.class_id,
        is_logged_in=True,
    )
    print(f"login ok; parent_account_id={account.parent_id}; ecard={account.name or account.id or account.key}")


def command_list(args: argparse.Namespace) -> None:
    client = make_client(args)
    token, parent_id = ensure_auth(client)
    response = client.get_homework_list(token, parent_id, args.status)
    for index, homework in enumerate(response.homeworks):
        print(f"[{index}] {homework.name} ({len(homework.contents)} resources)")
        for content in homework.contents:
            print(f"    - {content.group_name}: {content.url}")


def command_fetch(args: argparse.Namespace) -> None:
    client = make_client(args)
    token, parent_id = ensure_auth(client)
    response = client.get_homework_list(token, parent_id, args.status)
    if args.homework_index < 0 or args.homework_index >= len(response.homeworks):
        raise ETS100Error(f"homework index out of range: {args.homework_index}")
    homework = response.homeworks[args.homework_index]
    paper = fetch_homework(client, response, homework)
    out = Path(args.out).expanduser().resolve() if args.out else client.store.cache_dir / f"{sanitize_file_name(homework.name)}.answers.json"
    save_json(out, dataclasses.asdict(paper))
    print(f"saved {out}")
    if args.images:
        image_dir = Path(args.image_dir).expanduser().resolve() if args.image_dir else out.parent / "images"
        image_paths = render_papers_to_images([dataclasses.asdict(paper)], image_dir)
        for image_path in image_paths:
            print(f"saved image {image_path}")


def command_fetch_all(args: argparse.Namespace) -> None:
    client = make_client(args)
    token, parent_id = ensure_auth(client)
    response = client.get_homework_list(token, parent_id, args.status)
    papers: list[Paper] = []
    errors: list[dict[str, Any]] = []

    for index, homework in enumerate(response.homeworks):
        print(f"[{index}] fetching {homework.name} ({len(homework.contents)} resources)...")
        try:
            papers.append(fetch_homework(client, response, homework))
        except Exception as exc:
            errors.append(
                {
                    "index": index,
                    "homework_id": homework.id,
                    "homework_name": homework.name,
                    "error": str(exc),
                }
            )
            print(f"[{index}] failed: {exc}", file=sys.stderr)
            if not args.continue_on_error:
                raise ETS100Error(
                    "fetch-all stopped after a failed homework; "
                    "rerun with --continue-on-error to save a partial result with errors"
                ) from exc

    out = (
        Path(args.out).expanduser().resolve()
        if args.out
        else Path.cwd() / "results" / f"{time.strftime('%Y-%m-%d')}_all_answers.json"
    )
    save_json(
        out,
        {
            "status": args.status,
            "base_url": response.base_url,
            "fetched_count": len(papers),
            "error_count": len(errors),
            "papers": [dataclasses.asdict(paper) for paper in papers],
            "errors": errors,
        },
    )
    if errors:
        print(f"saved partial result {out} ({len(errors)} errors)")
    else:
        print(f"saved {out}")
    if args.images:
        image_dir = Path(args.image_dir).expanduser().resolve() if args.image_dir else out.parent / "images"
        image_paths = render_papers_to_images([dataclasses.asdict(paper) for paper in papers], image_dir)
        for image_path in image_paths:
            print(f"saved image {image_path}")


def command_parse_local(args: argparse.Namespace) -> None:
    section = content_to_section(Path(args.path), args.group_name or "local")
    save_json(Path(args.out), dataclasses.asdict(section)) if args.out else print(
        json.dumps(dataclasses.asdict(section), ensure_ascii=False, indent=2)
    )


def command_render_images(args: argparse.Namespace) -> None:
    input_path = Path(args.input).expanduser().resolve()
    data = json.loads(input_path.read_text(encoding="utf-8"))
    raw_papers = data.get("papers") if isinstance(data, dict) else None
    if raw_papers is None and isinstance(data, dict) and data.get("sections"):
        raw_papers = [data]
    if not isinstance(raw_papers, list):
        raise ETS100Error("input JSON must be a fetch-all result with papers[] or a single paper object")
    papers = [paper for paper in raw_papers if isinstance(paper, dict)]
    image_dir = Path(args.image_dir).expanduser().resolve() if args.image_dir else input_path.parent / "images"
    image_paths = render_papers_to_images(papers, image_dir)
    for image_path in image_paths:
        print(f"saved image {image_path}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="ETS100 cloud mode Python implementation")
    parser.add_argument("--root", help="state/cache directory; default is outputs/.ets100_cloud")
    parser.add_argument("--verbose", action="store_true")
    subparsers = parser.add_subparsers(dest="command", required=True)

    login = subparsers.add_parser("login", help="login and save auth state")
    login.add_argument("--phone", required=True)
    login.add_argument("--password", required=True)
    login.add_argument("--ecard-id", help="preferred ecard id")
    login.add_argument("--save-password", action="store_true", help="store password for your own automation")
    login.set_defaults(func=command_login)

    list_cmd = subparsers.add_parser("list", help="list cloud homework")
    list_cmd.add_argument("--status", default=STATUS_CURRENT, choices=[STATUS_CURRENT, STATUS_HISTORY, STATUS_EXPIRED])
    list_cmd.set_defaults(func=command_list)

    fetch = subparsers.add_parser("fetch", help="download and parse one homework")
    fetch.add_argument("--homework-index", type=int, required=True)
    fetch.add_argument("--status", default=STATUS_CURRENT, choices=[STATUS_CURRENT, STATUS_HISTORY, STATUS_EXPIRED])
    fetch.add_argument("--out", help="output JSON path")
    fetch.add_argument("--insecure-cdn", action="store_true", help="skip CDN TLS verification")
    fetch.add_argument("--images", action="store_true", help="also render one answer PNG for the fetched homework")
    fetch.add_argument("--image-dir", help="directory for rendered answer PNG files")
    fetch.set_defaults(func=command_fetch)

    fetch_all = subparsers.add_parser("fetch-all", help="download and parse all homework for a status")
    fetch_all.add_argument("--status", default=STATUS_CURRENT, choices=[STATUS_CURRENT, STATUS_HISTORY, STATUS_EXPIRED])
    fetch_all.add_argument(
        "--out",
        help="output JSON path; default: results/YYYY-MM-DD_all_answers.json",
    )
    fetch_all.add_argument("--insecure-cdn", action="store_true", help="skip CDN TLS verification")
    fetch_all.add_argument("--images", action="store_true", help="also render one answer PNG per homework")
    fetch_all.add_argument("--image-dir", help="directory for rendered answer PNG files")
    fetch_all.add_argument(
        "--continue-on-error",
        action="store_true",
        help="keep fetching remaining homework if one homework fails",
    )
    fetch_all.set_defaults(func=command_fetch_all)

    parse = subparsers.add_parser("parse-local", help="parse a local content.json")
    parse.add_argument("path")
    parse.add_argument("--group-name")
    parse.add_argument("--out")
    parse.set_defaults(func=command_parse_local)

    render_images = subparsers.add_parser("render-images", help="render answer PNG files from an existing JSON result")
    render_images.add_argument("input", help="fetch/fetch-all JSON path")
    render_images.add_argument("--image-dir", help="directory for rendered answer PNG files")
    render_images.set_defaults(func=command_render_images)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        args.func(args)
        return 0
    except ETS100Error as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
