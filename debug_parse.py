import re

def debug_parse(filepath):
    with open(filepath, 'r', encoding='utf-8') as f:
        for i, line in enumerate(f):
            line = line.rstrip('\n')
            print(f'{i:3}: {repr(line)}')
            school_match = re.match(r'^##\s*(.+?)\s*（共\d+人）', line)
            if school_match:
                print(f'     -> school: {school_match.group(1)}')
                continue
            student_match = re.match(r'^\s*\d+\.\s*(.+)$', line)
            if student_match:
                print(f'     -> student: {student_match.group(1)}')
            if i > 30:
                break

if __name__ == '__main__':
    debug_parse('/home/zhangyu/github/zizhao/2026/art.txt')