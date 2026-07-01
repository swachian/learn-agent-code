import re

def test_regex():
    line = '## 上海市格致中学（共 7 人）'
    print('Line:', repr(line))
    # Try pattern with optional spaces
    pattern1 = r'^##\s*(.+?)（\s*\d+\s*人）'
    m = re.match(pattern1, line)
    if m:
        print('Match:', m.group(1))
    else:
        print('No match with pattern1')
    # Also try without spaces
    pattern2 = r'^##\s*(.+?)（\d+人）'
    m2 = re.match(pattern2, line)
    if m2:
        print('Match2:', m2.group(1))
    else:
        print('No match2')

if __name__ == '__main__':
    test_regex()