// color("#B4FBB8") square();
// color([255, 0, 0, 128]) translate([0.5, 0,]) square();
// color([255, 0, 0]) translate([5, 0, 10]) square();
// color([255, 0, 0]) translate([5, 0, 10]) square();
// color("#23a9dd") translate([10, 0, 10]) circle(r=2);

// color("blue") polygon([[0,0],[100,0],[130,50],[30,50]], paths=[[0,1,2,3]]);

for(i=[0:36])
  for(j=[0:36])
    color([0.5+sin(10*i)/2, 0.5+sin(10*j)/2, 0.5+sin(10*(i+j))/2])
      translate([i, j, 0])
        cube([1, 1, 11+10*cos(10*i)*sin(10*j)]);