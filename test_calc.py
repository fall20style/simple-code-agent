
import calc

assert calc.add(2, 3) == 5
assert calc.subtract(7, 4) == 3
assert calc.multiply(6, 7) == 42
try:
    assert calc.divide(8, 0) == 4
except ValueError as e:
    print(e)
print("ALL TESTS PASSED")
