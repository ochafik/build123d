// Focused test: transforms — translate, rotate, uniform & non-uniform scale, mirror.

cube([4, 4, 4]);

translate([10, 0, 0])
  rotate([0, 0, 45])
    cube([4, 4, 4]);

// uniform scale (rigid-ish — handled exactly)
translate([20, 0, 0])
  scale(1.5) sphere(2);

// non-uniform scale — must rewrite the BREP (gp_GTrsf)
translate([0, 12, 0])
  scale([2, 1, 1]) cube([3, 3, 3]);

// mirror
translate([12, 12, 0])
  mirror([1, 0, 0]) translate([2, 0, 0]) cube([3, 3, 3]);
