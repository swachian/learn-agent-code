import pandas as pd
import re

def parse_txt(filepath):
    """Parse the txt file and return dict school->对口小学."""
    school_to_primary = {}
    with open(filepath, 'r', encoding='utf-8') as f:
        lines = f.readlines()
    # Find start of table: line that starts with '|' and contains '学校名称'
    start = -1
    for i, line in enumerate(lines):
        if line.strip().startswith('|') and '学校名称' in line:
            start = i
            break
    if start == -1:
        return school_to_primary
    # Process lines after start+2 (skip header and separator line)
    for line in lines[start+2:]:
        stripped = line.strip()
        if not stripped.startswith('|'):
            break  # end of table
        # Remove leading/trailing '|'
        parts = [p.strip() for p in stripped.strip('|').split('|')]
        if len(parts) < 4:
            continue
        # parts: 序号, 学校名称, 择招班级/拟招班级, 对口小学
        school = parts[1]
        primary = parts[3] if len(parts) > 3 else ''
        school_to_primary[school] = primary
    return school_to_primary

def main():
    dict_2018 = parse_txt('.data/2018.txt')
    dict_2026 = parse_txt('.data/2026.txt')
    
    all_schools = set(dict_2018.keys()) | set(dict_2026.keys())
    rows = []
    for school in sorted(all_schools):
        rows.append({
            '学校名称': school,
            '2018对口小学': dict_2018.get(school, ''),
            '2026对口小学': dict_2026.get(school, '')
        })
    df = pd.DataFrame(rows)
    output_path = '.data/school_merge.xlsx'
    df.to_excel(output_path, index=False)
    print(f'Saved merged data to {output_path}')
    print(df.head())

if __name__ == '__main__':
    main()