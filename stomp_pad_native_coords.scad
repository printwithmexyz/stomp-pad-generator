// ==================================================================
// CRAFTED CARVING - Auto-Generated Stomp Pad
// ==================================================================
// Pyramid positions calculated in SVG's native coordinate space
// Positions pre-rotated by 270° if needed
// Positions will be scaled to match OpenSCAD's resize() operation

// ===== PARAMETERS =====
svg_file = "Snake6-01.svg";
target_width = 100;

// Scale factor to match resize() operation
position_scale = 0.17525123284359476;

base_thickness = 2;
outline_offset = 2;
outline_height = 0.8;

pyramid_size = 4;
pyramid_height = 2.5;
pyramid_style = 4;

corner_rounding = 0.5;
total_height = base_thickness + outline_height;

// Pyramid positions in SVG's native coordinate space
// These will be scaled by position_scale to match the resized SVG
pyramid_positions_svg = [
    [135.492, 494.374],
    [135.492, 465.844],
    [160.200, 508.640],
    [160.200, 480.109],
    [160.200, 451.579],
    [184.907, 465.844],
    [184.907, 437.314],
    [234.322, 380.253],
    [234.322, 209.070],
    [234.322, 180.539],
    [234.322, 152.009],
    [259.029, 251.865],
    [259.029, 223.335],
    [259.029, 194.805],
    [259.029, 137.744],
    [259.029, 109.213],
    [283.737, 380.253],
    [283.737, 266.131],
    [283.737, 237.600],
    [283.737, 94.948],
    [308.444, 394.518],
    [308.444, 280.396],
    [308.444, 251.865],
    [308.444, 109.213],
    [308.444, 80.683],
    [333.151, 408.783],
    [333.151, 266.131],
    [333.151, 94.948],
    [357.859, 423.048],
    [357.859, 280.396],
    [357.859, 109.213],
    [382.566, 437.314],
    [382.566, 294.661],
    [382.566, 123.478],
    [407.273, 451.579],
    [407.273, 423.048],
    [407.273, 308.926],
    [407.273, 166.274],
    [407.273, 137.744],
    [431.981, 437.314],
    [431.981, 408.783],
    [431.981, 323.192],
    [431.981, 180.539],
    [456.688, 423.048],
    [456.688, 394.518],
    [456.688, 365.987],
    [456.688, 337.457],
    [456.688, 223.335],
    [456.688, 194.805],
    [456.688, 80.683],
    [481.396, 237.600],
    [506.103, 251.865],
    [530.810, 266.131],
    [555.518, 280.396],
    [555.518, 137.744],
    [580.225, 152.009],
    [604.933, 280.396],
    [604.933, 251.865],
    [604.933, 166.274],
    [629.640, 237.600],
    [629.640, 209.070]
];

// ===== MODULES =====

module scaled_svg() {
    resize([target_width, 0, 0], auto=true) {
        import(svg_file);
    }
}

module base_shape_2d() {
    scaled_svg();
}

module outlined_shape_2d() {
    if (corner_rounding > 0) {
        offset(r = corner_rounding) {
            offset(r = outline_offset - corner_rounding) {
                base_shape_2d();
            }
        }
    } else {
        offset(r = outline_offset) {
            base_shape_2d();
        }
    }
}

module grip_pyramid() {
    rotate([0, 0, 45]) {
        cylinder(
            h = pyramid_height,
            r1 = pyramid_size / 1.414,
            r2 = 0,
            $fn = pyramid_style
        );
    }
}

// Place pyramids at pre-calculated and pre-rotated positions
module pyramids_on_surface() {
    for (pos = pyramid_positions_svg) {
        translate([pos[0] * position_scale, pos[1] * position_scale, total_height]) {
            grip_pyramid();
        }
    }
}

// ===== ASSEMBLY =====

module stomp_pad_complete() {
    union() {
        linear_extrude(height = base_thickness) {
            base_shape_2d();
        }

        difference() {
            linear_extrude(height = total_height) {
                outlined_shape_2d();
            }
            translate([0, 0, base_thickness]) {
                linear_extrude(height = total_height) {
                    base_shape_2d();
                }
            }
        }

        pyramids_on_surface();
    }
}

stomp_pad_complete();
