import os
import re
import json
import subprocess
import shlex
import hashlib
import pandas as pd
from dataclasses import dataclass
from pathlib import Path
import base64

from openai import OpenAI
from dotenv import load_dotenv

load_dotenv(override=True)


client = OpenAI(
    base_url="https://integrate.api.nvidia.com/v1",
    api_key=os.getenv("NVIDIA_API_KEY"),
)

WORKDIR = Path.cwd()
MODEL = os.getenv("MODEL_ID", "moonshotai/kimi-k2.5")
SKILLS_DIR = WORKDIR / "skills"

import pandas as pd
data_file = "2024年资格_关联2026年优秀名单.xlsx"

def process_excel(xlsx_path, school_map, output_path):
    df = pd.read_excel(xlsx_path)

    # 确保列存在
    if "姓名" not in df.columns:
        raise ValueError("Excel中没有‘姓名’列")

    if "资格确认学校" not in df.columns:
        df["资格确认学校"] = ""

    df["资格确认学校"] = df["资格确认学校"].astype("object")
    df["姓名"] = df["姓名"].astype(str).str.strip()
    
    name_to_schools = {}

    for school, names in school_map.items():
        clean_names = {n.strip() for n in names}

        for name in clean_names:
            if name not in name_to_schools:
                name_to_schools[name] = []
            name_to_schools[name].append(school)
    
    def merge_schools(name):
        schools = name_to_schools.get(name)
        if not schools:
            return ""
        return "+".join(schools)

    # 匹配并填值
    df["资格确认学校"] = df["姓名"].apply(merge_schools)

    df.to_excel(output_path, index=False)
    

CACHE_DIR = ".cache_names"

def get_cache_key(image_path):
    # 用文件内容做 hash（更稳，不怕文件名变）
    with open(image_path, "rb") as f:
        return hashlib.md5(f.read()).hexdigest()
    
    
def extract_names_from_image(image_path):
    os.makedirs(CACHE_DIR, exist_ok=True)
    
    key = get_cache_key(image_path)
    cache_file = os.path.join(CACHE_DIR, key + ".json")
    
    # 命中缓存
    if os.path.exists(cache_file):
        with open(cache_file, "r") as f:
            print(f"[CACHE HIT] {image_path}")
            return json.load(f)
    
    print(f"[LLM CALL] {image_path}")
    with open(image_path, "rb") as f:
        img_b64 = base64.b64encode(f.read()).decode()

    prompt = """
请识别图片中的表格，只返回第二列“姓名”的所有内容。
要求：
- 只输出姓名列表
- 每行一个姓名
- 不要解释
"""

    resp = client.chat.completions.create(
        model=MODEL,  
        messages=[
            {"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{img_b64}"}}
            ]}
        ]
    )

    text = resp.choices[0].message.content
    names = [line.strip() for line in text.splitlines() if line.strip()]
    print(names)
    with open(cache_file, "w") as f:
        json.dump(names, f, ensure_ascii=False, indent=2)
    return names    
    
    
def run_agent():
    school_map = {
        "交附嘉定": (
            extract_names_from_image("image_jfjd1.png") +
            extract_names_from_image("image_jfjd2.png")
        ),
        "复旦附中": (
            extract_names_from_image("image_fdfz1.png") +
            extract_names_from_image("image_fdfz2.png")
        ),
        "控江中学": (
            extract_names_from_image("image_kjzx.png")
        ),
        "南洋模范": (
            extract_names_from_image("image_nymf.png")
        ),
        "建平中学": (
            extract_names_from_image("image_jpzx.png")
        )
    }

    process_excel(
        "2024年资格_关联2026年优秀名单.xlsx",
        school_map,
        "output.xlsx"
    )

    print("完成：output.xlsx 已生成")    
    
run_agent()