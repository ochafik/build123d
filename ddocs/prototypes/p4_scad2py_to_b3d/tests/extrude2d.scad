// Focused test: linear_extrude of 2D primitives + 2D booleans.
// Exercises csg.LinearExtrude, csg.Polygon (square/circle), and 2D difference.

// plain extrude of a square
linear_extrude(10) square([6, 4], center=true);

// extrude of a 2D difference (ring)
translate([20, 0, 0])
  linear_extrude(5)
    difference() {
      circle(8);
      circle(5);
    }

// extrude with uniform scale (taper)
translate([0, 20, 0])
  linear_extrude(10, scale=0.3) square(8, center=true);
