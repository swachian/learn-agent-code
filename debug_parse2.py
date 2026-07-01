import re

def debug_parse(filepath):
    with open(filepath, 'r', encoding='utf-8') as f:
        lines = f.readlines()
    student_to_school = {}
    current_school = None
    for i, line in enumerate(lines):
        line = line.rstrip('\n')
        school_match = re.match(r'^##\s*(.+?)（共\d+人）', line)
        if school_match:
            current_school = school_match.group(1).strip()
            print(f'School line {i}: {repr(line)} -> {current_school}')
            continue
        student_match = re.match(r'^\s*\d+\.\s*(.+)$', line)
        if student_match and current_school:
            name = student_match.group(1).strip()
            student_to_school[name] = current_school
            if len(student_to_school) < 10:
                print(f'Student line {i}: {repr(line)} -> name={repr(name)} -> school={repr(current_school)}')
    print(f'Parsed {len(student_to_school)} students')
    # Show first few
    for name, school in list(student_to_school.items())[:5]:
        print(f'  {name} -> {school}')
    return student_to_school

if __name__ == '__main__':
    debug_parse('/home/zhangyu/github/zizhao/2026/art.txt')