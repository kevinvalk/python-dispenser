from datetime import datetime, timedelta
import re
import statistics


regex = re.compile('.*(?P<days>[0-9]+):(?P<minutes>[0-9]{2,2}):(?P<seconds>[0-9]{2,2})\.(?P<microseconds>[0-9]+)')

falling_edge = 0
raising_edge = 0

def get_time(line):
	r = regex.match(line)
	kwargs = {k: int(v) for k, v in r.groupdict().items()}
	return timedelta(**kwargs)


# 32282
big = timedelta(milliseconds=300)
big_holes = []
falling_edges = []
raising_edges = []
with open('run_empty_1000.log') as f:
	for line in f:
		if line[0] == '#':
			continue

		if 'Falling edge' in line:
			falling_edge += 1

			t = get_time(line)
			if t > big:
				big_holes.append(t)
			else:
				falling_edges.append(t)
		elif 'Raising edge' in line:
			raising_edge += 1

			raising_edges.append(get_time(line))


print(f'Big holes  {len(big_holes)}')
print(min(big_holes))
print(max(big_holes))
print()



print(f'{falling_edge}  {len(falling_edges)}')
print(min(falling_edges))
print(max(falling_edges))
print()

print(f'{raising_edge} {len(raising_edges)}')
print(min(raising_edges))
print(max(raising_edges))
print()

# diffs = []
# for i in range(0, len(raising_edges), 2):
# 	l = raising_edges[i]
# 	r = raising_edges[i + 1]

# 	diff = abs(1 - (l.total_seconds() / r.total_seconds()))
# 	diffs.append(diff)
# 	print(round(diff * 100, 2), l, r)

# print(f'{len(diffs)}')
# print(min(diffs))
# print(max(diffs))
# print()

#         50%
#         29
#        +---+
#        |   |
# -------+   +------
#  119
#  25%
