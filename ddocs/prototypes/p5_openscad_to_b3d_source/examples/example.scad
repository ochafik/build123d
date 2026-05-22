// include <bar.scad>
// use <baz.scad>

n=10;

function foo(x) =
  x * x;

module foo(x) {
  // for (i=[0:2:foo(x)], j=[-3:0.1:30])
  for (i=[0:2:foo(x)], j=[-3:0.1:1])
    translate([i, j, 0])
      cube(0.5);
}

foo(10);
foo(20);