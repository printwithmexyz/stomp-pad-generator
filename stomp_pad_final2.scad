// ==================================================================
// CRAFTED CARVING - Auto-Generated Stomp Pad
// ==================================================================
// Source: Snake6-01.svg
// Generated: 61 valid pyramid positions
// Method: Python pre-calculation with boundary testing
//
// ZERO partial pyramids - mathematically guaranteed!
// ==================================================================

// ===== PARAMETERS =====
svg_file = "Snake6-01.svg";
target_width = 100;
maintain_aspect_ratio = true;

base_thickness = 2;
outline_offset = 2;
outline_height = 0.8;

pyramid_size = 4;
pyramid_height = 2.5;
pyramid_style = 4;

corner_rounding = 0.5;
total_height = base_thickness + outline_height;

// ===== PRE-CALCULATED VALID PYRAMID POSITIONS =====
// These 61 positions were tested and verified to fit
// completely inside the SVG boundary

valid_pyramid_positions = [
    [7.500, 4.330],
    [12.500, 4.330],
    [5.000, 8.660],
    [10.000, 8.660],
    [15.000, 8.660],
    [12.500, 12.990],
    [17.500, 12.990],
    [27.500, 21.650],
    [57.500, 21.650],
    [62.500, 21.650],
    [67.500, 21.650],
    [50.000, 25.980],
    [55.000, 25.980],
    [60.000, 25.980],
    [70.000, 25.980],
    [75.000, 25.980],
    [27.500, 30.310],
    [47.500, 30.310],
    [52.500, 30.310],
    [77.500, 30.310],
    [25.000, 34.640],
    [45.000, 34.640],
    [50.000, 34.640],
    [75.000, 34.640],
    [80.000, 34.640],
    [22.500, 38.970],
    [47.500, 38.970],
    [77.500, 38.970],
    [20.000, 43.300],
    [45.000, 43.300],
    [75.000, 43.300],
    [17.500, 47.630],
    [42.500, 47.630],
    [72.500, 47.630],
    [15.000, 51.960],
    [20.000, 51.960],
    [40.000, 51.960],
    [65.000, 51.960],
    [70.000, 51.960],
    [17.500, 56.290],
    [22.500, 56.290],
    [37.500, 56.290],
    [62.500, 56.290],
    [20.000, 60.620],
    [25.000, 60.620],
    [30.000, 60.620],
    [35.000, 60.620],
    [55.000, 60.620],
    [60.000, 60.620],
    [80.000, 60.620],
    [52.500, 64.950],
    [50.000, 69.280],
    [47.500, 73.610],
    [45.000, 77.940],
    [70.000, 77.940],
    [67.500, 82.270],
    [45.000, 86.600],
    [50.000, 86.600],
    [65.000, 86.600],
    [52.500, 90.930],
    [57.500, 90.930]
];

// ===== MODULES =====

module scaled_svg() {
    resize([target_width, 0, 0], auto=maintain_aspect_ratio) {
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

// Place pyramids ONLY at validated positions
module pyramids_on_surface() {
    for (pos = valid_pyramid_positions) {
        translate([pos[0], pos[1], total_height]) {
            grip_pyramid();
        }
    }
}

// ===== ASSEMBLY =====

module stomp_pad_complete() {
    union() {
        // Base: Logo shape (flat)
        linear_extrude(height = base_thickness) {
            base_shape_2d();
        }

        // Raised outline border
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

        // Grip pyramids - NO PARTIALS!
        pyramids_on_surface();
    }
}

stomp_pad_complete();

// ==================================================================
// Ready to render! Press F6 in OpenSCAD
// ==================================================================
