import re

def parse_art_txt(filepath):
    student_to_school = {}
    current_school = None
    with open(filepath, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.rstrip('\n')
            # Match school line: starts with '## ' and contains '（共' and '人）'
            if line.startswith('## '):
                # Remove the leading '## '
                rest = line[3:]
                # Split by the first occurrence of '（共'
                if '（共' in rest:
                    school_part, rest_after = rest.split('（共', 1)
                    school_name = school_part.strip()
                    # Extract the number from rest_after (should be like ' 7 人）')
                    # We'll just use the school_name as current school
                    current_school = school_name
                    # print(f'School: {current_school}')
                else:
                    # Fallback: take the whole line after '## ' as school name
                    current_school = rest.strip()
                continue
            # Match student line: starts with optional spaces, then digits, then '.', then space, then name
            student_match = re.match(r'^\s*\d+\.\s*(.+)$', line)
            if student_match and current_school:
                name = student_match.group(1).strip()
                student_to_school[name] = current_school
                # print(f'Student: {name} -> {current_school}')
    return student_to_school

if __name__ == '__main__':
    mapping = parse_art_txt('/home/zhangyu/github/zizhao/2026/art.txt')
    print(f'Parsed {len(mapping)} students')
    for name, school in list(mapping.items())[:5]:
        print(f'  {name} -> {school}')
