import os
import re
import json
import subprocess
import shlex
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

def process_excel(xlsx_path, names, output_path):
    df = pd.read_excel(xlsx_path)

    # 确保列存在
    if "姓名" not in df.columns:
        raise ValueError("Excel中没有‘姓名’列")

    if "资格确认学校" not in df.columns:
        df["资格确认学校"] = ""

    df["资格确认学校"] = df["资格确认学校"].astype("object")

    # 匹配并填值
    df.loc[df["姓名"].isin(names), "资格确认学校"] = "交附嘉定"

    df.to_excel(output_path, index=False)
    

    
def extract_names_from_image(image_path):
    print(f"extract names from {image_path}")
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
        model=MODEL,  # 或你自己的模型
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
    return names    
    
    
def run_agent():
    names1 = extract_names_from_image("image_jfjd1.png")
    names2 = extract_names_from_image("image_jfjd2.png")
    # names1 = ['李沐恩']
    # names2 = ['he']

    all_names = list(set(names1 + names2))

    process_excel(
        "2024年资格_关联2026年优秀名单.xlsx",
        all_names,
        "output.xlsx"
    )

    print("完成：output.xlsx 已生成")    
    
run_agent()