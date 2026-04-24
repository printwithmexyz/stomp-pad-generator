#!/usr/bin/env python3
"""
SVG-Based Pyramid Position Calculator
Pre-calculates which pyramid positions fit inside the SVG boundary
Uses centerline/skeleton-based hexagonal packing
Generates OpenSCAD code with ONLY valid positions
"""

import numpy as np
import shapely
from shapely.geometry import Point, Polygon
from svg.path import parse_path
from skimage.morphology import medial_axis

# Prefer defusedxml on untrusted input. stdlib ET disables external entity
# expansion since 3.7.1, but defusedxml is the documented best practice and
# matters now that the same calculator is used by the public web version.
try:
    import defusedxml.ElementTree as ET
except ImportError:
    import xml.etree.ElementTree as ET


def _log(logger, message):
    """Emit a message via the supplied logger callback, falling back to print."""
    (logger or print)(message)


def parse_svg_to_polygon(svg_file, target_width=100, target_height=None, samples_per_segment=20, logger=None):
    """
    Parse SVG file and convert to Shapely polygon
    Returns (polygon, svg_viewbox_info) where polygon is scaled to fit within target dimensions
    Properly samples curves and ensures closed paths

    Args:
        svg_file: Path to SVG file
        target_width: Target width in mm (X dimension)
        target_height: Target height in mm (Y dimension), or None to scale by width only
        samples_per_segment: Number of sample points per curve segment

    Scaling behavior:
        - If only target_width is specified: scale to match width (maintain aspect ratio)
        - If both target_width and target_height are specified: scale to fit within both
          constraints, using the smaller scale factor to maintain aspect ratio
    """
    tree = ET.parse(svg_file)
    root = tree.getroot()

    # Get SVG viewBox for coordinate system reference
    viewBox = root.get('viewBox')
    svg_x_offset = 0
    svg_y_offset = 0
    svg_width = None
    svg_height = None

    if viewBox:
        vb = [float(x) for x in viewBox.split()]
        svg_x_offset = vb[0]
        svg_y_offset = vb[1]
        svg_width = vb[2]
        svg_height = vb[3]

    # Extract and sample all supported shape elements. SVGs in the wild use
    # <path> most of the time, but Figma / icon sets emit primitives directly.
    NS = '{http://www.w3.org/2000/svg}'
    all_points = []

    def _close(pts):
        if len(pts) > 2:
            first = np.array(pts[0])
            last = np.array(pts[-1])
            if np.linalg.norm(first - last) > 0.1:
                pts.append(pts[0])
        return pts

    for path_elem in root.findall(f'.//{NS}path'):
        d = path_elem.get('d')
        if not d:
            continue
        path = parse_path(d)
        path_points = []
        for segment in path:
            for i in range(samples_per_segment):
                t = i / samples_per_segment
                point = segment.point(t)
                path_points.append((point.real, point.imag))
        if path:
            final_point = path[-1].end
            path_points.append((final_point.real, final_point.imag))
        all_points.extend(_close(path_points))

    for poly_elem in root.findall(f'.//{NS}polygon'):
        pts_str = poly_elem.get('points', '').replace(',', ' ').split()
        coords = [float(v) for v in pts_str]
        pts = [(coords[i], coords[i + 1]) for i in range(0, len(coords) - 1, 2)]
        if len(pts) >= 3:
            all_points.extend(_close(pts))

    for rect_elem in root.findall(f'.//{NS}rect'):
        x = float(rect_elem.get('x', 0))
        y = float(rect_elem.get('y', 0))
        w = float(rect_elem.get('width', 0))
        h = float(rect_elem.get('height', 0))
        if w > 0 and h > 0:
            all_points.extend([(x, y), (x + w, y), (x + w, y + h), (x, y + h), (x, y)])

    circle_samples = max(samples_per_segment * 2, 64)
    for circle_elem in root.findall(f'.//{NS}circle'):
        cx = float(circle_elem.get('cx', 0))
        cy = float(circle_elem.get('cy', 0))
        r = float(circle_elem.get('r', 0))
        if r > 0:
            angles = np.linspace(0, 2 * np.pi, circle_samples, endpoint=True)
            all_points.extend(zip(
                (cx + r * np.cos(angles)).tolist(),
                (cy + r * np.sin(angles)).tolist(),
            ))

    for ellipse_elem in root.findall(f'.//{NS}ellipse'):
        cx = float(ellipse_elem.get('cx', 0))
        cy = float(ellipse_elem.get('cy', 0))
        rx = float(ellipse_elem.get('rx', 0))
        ry = float(ellipse_elem.get('ry', 0))
        if rx > 0 and ry > 0:
            angles = np.linspace(0, 2 * np.pi, circle_samples, endpoint=True)
            all_points.extend(zip(
                (cx + rx * np.cos(angles)).tolist(),
                (cy + ry * np.sin(angles)).tolist(),
            ))

    if len(all_points) < 3:
        _log(logger, f"ERROR: Not enough points parsed from SVG ({len(all_points)} points)")
        return None

    # Create polygon from sampled points
    poly = Polygon(all_points)

    if not poly.is_valid:
        # Try to fix invalid polygon
        poly = poly.buffer(0)
        if not poly.is_valid:
            _log(logger, "ERROR: Could not create valid polygon from SVG")
            return None

    # Get original bounds
    bounds = poly.bounds
    original_min_x, original_min_y, original_max_x, original_max_y = bounds
    original_width = original_max_x - original_min_x
    original_height = original_max_y - original_min_y

    from shapely.affinity import scale as shapely_scale

    # Calculate scale factor based on target dimensions
    # IMPORTANT: Scale from origin (0,0) to match OpenSCAD's resize() behavior
    # OpenSCAD resize() scales the SVG but keeps it in its original coordinate space
    # We do NOT translate to origin - we keep the SVG viewBox coordinates

    scale_factor_width = target_width / original_width

    if target_height is not None and target_height > 0:
        # Both width and height constraints specified
        # Use the smaller scale factor to fit within both constraints
        scale_factor_height = target_height / original_height
        scale_factor = min(scale_factor_width, scale_factor_height)
        scaling_mode = "fit-to-bounds"
        if scale_factor == scale_factor_width:
            limiting_dim = "width"
        else:
            limiting_dim = "height"
    else:
        # Only width constraint - scale to match width
        scale_factor = scale_factor_width
        scaling_mode = "fit-to-width"
        limiting_dim = "width"

    poly_scaled = shapely_scale(poly, xfact=scale_factor, yfact=scale_factor, origin=(0, 0))

    bounds_scaled = poly_scaled.bounds
    final_width = bounds_scaled[2] - bounds_scaled[0]
    final_height = bounds_scaled[3] - bounds_scaled[1]

    _log(logger, f"  SVG parsed: {len(all_points)} points sampled")
    _log(logger, f"  Original size: {original_width:.1f} x {original_height:.1f}")
    _log(logger, f"  Scaling mode: {scaling_mode} (limited by {limiting_dim})")
    _log(logger, f"  Scale factor: {scale_factor:.4f}")
    _log(logger,
         f"  Scaled bounds: ({bounds_scaled[0]:.1f}, {bounds_scaled[1]:.1f}) to ({bounds_scaled[2]:.1f}, {bounds_scaled[3]:.1f})")
    _log(logger, f"  Final size: {final_width:.1f} x {final_height:.1f}")

    # Return polygon and SVG viewBox info (scaled)
    svg_info = {
        'viewbox_width': svg_width * scale_factor if svg_width else final_width,
        'viewbox_height': svg_height * scale_factor if svg_height else final_height,
        'scale_factor': scale_factor,
        'final_width': final_width,
        'final_height': final_height
    }

    return poly_scaled, svg_info


def calculate_skeleton(polygon, resolution=0.5):
    """
    Calculate the centerline/skeleton of the polygon using medial axis transform.
    Returns a list of (x, y) skeleton points.

    Vectorized: shapely.contains_xy over a meshgrid is ~50-100x faster than
    polygon.contains(Point(x, y)) per pixel, and matters a lot in Pyodide
    where Python loops are uncached interpreted bytecode.
    """
    min_x, min_y, max_x, max_y = polygon.bounds
    width = int((max_x - min_x) / resolution) + 1
    height = int((max_y - min_y) / resolution) + 1

    grid_x = min_x + np.arange(width) * resolution
    grid_y = min_y + np.arange(height) * resolution
    xx, yy = np.meshgrid(grid_x, grid_y)
    binary_image = shapely.contains_xy(polygon, xx.ravel(), yy.ravel()).reshape(height, width)

    skeleton = medial_axis(binary_image)

    iy, jx = np.where(skeleton)
    xs = min_x + jx * resolution
    ys = min_y + iy * resolution
    return list(zip(xs.tolist(), ys.tolist()))


def calculate_centerline_tangent(skeleton_points, x, y, sample_distance=3.0):
    """
    Calculate the tangent direction at a point along the centerline
    Returns rotation angle in degrees for pyramid alignment
    This is the TRUE orientation for visualization - OpenSCAD will adjust it
    """
    if not skeleton_points:
        return 0.0

    # Find nearest skeleton point
    point = np.array([x, y])
    skeleton_array = np.array(skeleton_points)
    distances = np.linalg.norm(skeleton_array - point, axis=1)
    nearest_idx = np.argmin(distances)

    # Get local neighborhood of skeleton points
    nearby_indices = np.where(distances < sample_distance)[0]

    if len(nearby_indices) < 2:
        return 0.0

    # Fit a line through nearby skeleton points to get tangent
    nearby_points = skeleton_array[nearby_indices]

    # Use PCA to find principal direction. eigh is faster and numerically
    # stable for symmetric matrices (covariance is symmetric).
    centered = nearby_points - nearby_points.mean(axis=0)
    cov = np.cov(centered.T)
    eigenvalues, eigenvectors = np.linalg.eigh(cov)

    # Principal component (direction of maximum variance)
    principal_direction = eigenvectors[:, np.argmax(eigenvalues)]

    # Calculate angle of the tangent - this is what we want the pyramid to align with
    tangent_angle = np.degrees(np.arctan2(principal_direction[1], principal_direction[0]))

    return tangent_angle


def create_pyramid_footprint(x, y, pyramid_size, rotation_deg=0):
    """
    Create a polygon representing the actual pyramid base (rotated square)
    """
    # Pyramid base is a square rotated 45 degrees, then rotated by rotation_deg
    half_size = pyramid_size / 2

    # Square corners at 45 degree angles (diamond orientation)
    base_angle = 45  # Base diamond orientation
    total_rotation = base_angle + rotation_deg

    corners = []
    for angle_offset in [0, 90, 180, 270]:
        angle_rad = np.radians(total_rotation + angle_offset)
        # Distance from center to corner of rotated square
        corner_dist = half_size * np.sqrt(2)
        corner_x = x + corner_dist * np.cos(angle_rad)
        corner_y = y + corner_dist * np.sin(angle_rad)
        corners.append((corner_x, corner_y))

    return Polygon(corners)


def calculate_valid_pyramid_positions(
        polygon,
        pyramid_size=4,
        pyramid_spacing=1,
        target_width=100,
        include_rotation=True,
        safety_margin=0.5,
        skeleton_points=None,
        skeleton_resolution=0.3,
        logger=None
):
    """
    Calculate pyramid positions using centerline/skeleton-based hexagonal packing
    Returns list of [x, y, rotation] positions (rotation in degrees)

    Args:
        polygon: The original SVG boundary polygon (not outlined)
        pyramid_size: Size of pyramid base
        pyramid_spacing: Spacing between pyramids
        safety_margin: Inset margin from boundary edge
    """
    if skeleton_points is None:
        _log(logger, "  Calculating skeleton/centerline...")
        skeleton_points = calculate_skeleton(polygon, resolution=skeleton_resolution)

    if not skeleton_points:
        _log(logger, "  WARNING: No skeleton points found")
        return []

    _log(logger, f"  Found {len(skeleton_points)} skeleton points")

    # Shrink polygon by safety margin to ensure pyramids stay inside
    polygon_safe = polygon.buffer(-safety_margin) if safety_margin > 0 else polygon

    pyramid_pitch = pyramid_size + pyramid_spacing
    # Hexagonal packing spacing
    hex_spacing_x = pyramid_pitch
    hex_spacing_y = pyramid_pitch * np.sqrt(3) / 2

    valid_positions = []

    # Get bounds for creating packing grid
    bounds = polygon.bounds
    min_x, min_y, max_x, max_y = bounds

    # Create hexagonal packing grid
    num_rows = int(np.ceil((max_y - min_y) / hex_spacing_y)) + 2
    num_cols = int(np.ceil((max_x - min_x) / hex_spacing_x)) + 2

    _log(logger, f"  Testing {num_rows * num_cols} potential positions...")

    for row in range(num_rows):
        # Offset every other row for hex pattern
        x_offset = (row % 2) * (hex_spacing_x / 2)

        for col in range(num_cols):
            x_pos = min_x + col * hex_spacing_x + x_offset
            y_pos = min_y + row * hex_spacing_y

            # Check if center point is inside the safe polygon
            center_point = Point(x_pos, y_pos)
            if not polygon_safe.contains(center_point):
                continue

            # Calculate rotation based on skeleton tangent
            if include_rotation:
                rotation = calculate_centerline_tangent(skeleton_points, x_pos, y_pos)
            else:
                rotation = 0

            # Create pyramid footprint with rotation
            pyramid_footprint = create_pyramid_footprint(x_pos, y_pos, pyramid_size, rotation)

            # IMPORTANT: Check against the ORIGINAL polygon (not outlined version)
            if polygon_safe.contains(pyramid_footprint):
                if include_rotation:
                    valid_positions.append([x_pos, y_pos, rotation])
                else:
                    valid_positions.append([x_pos, y_pos])

    _log(logger, f"  Valid positions: {len(valid_positions)}")
    return valid_positions


def generate_openscad_with_positions(
        svg_file,
        valid_positions,
        output_file,
        svg_info=None,
        logger=None,
        **params
):
    """
    Generate OpenSCAD file with pre-calculated valid pyramid positions

    Args:
        svg_info: Dictionary with SVG viewBox info (needed for Y-flip)
    """

    # Check if positions include rotation (3 elements vs 2)
    has_rotation = len(valid_positions[0]) == 3 if valid_positions else False

    # Get SVG viewBox height for Y-flip coordinate transformation
    # OpenSCAD imports SVG with Y-axis UP, but Python parses it with Y-axis DOWN
    # We need to flip Y-coordinates to match OpenSCAD's coordinate system
    # Use the SVG viewBox height (scaled) for proper transformation
    viewbox_height = svg_info['viewbox_height'] if svg_info else 0

    scad_code = f"""// AUTO-GENERATED by pyramid position calculator
// Valid pyramid positions pre-calculated from SVG boundary
//
// COORDINATE SYSTEM NOTES:
// - Python parses SVG with Y-axis DOWN (SVG native coordinates)
// - OpenSCAD imports SVG with Y-axis UP (standard 3D coordinates)
// - SVG coordinates are kept in viewBox space (not translated to origin)
// - Y coordinates are flipped: Y_openscad = viewbox_height - Y_python
// - X coordinates remain the same
//
// ROTATION NOTES:
// - Rotation angles calculated from skeleton tangent direction
// - Rotation is relative (tangent direction), so Y-flip doesn't affect it
// - Includes both path-following rotation AND 45° base diamond orientation
// - OpenSCAD applies the rotation directly (already includes diamond rotation)

// ===== PARAMETERS =====
svg_file = "{svg_file}";
// Final dimensions after scaling (calculated by Python)
final_width = {svg_info.get('final_width', params.get('target_width', 100)) if svg_info else params.get('target_width', 100)};
final_height = {svg_info.get('final_height', 0) if svg_info else 0};

base_thickness = {params.get('base_thickness', 2)};
outline_offset = {params.get('outline_offset', 2)};
outline_height = {params.get('outline_height', 0.8)};

pyramid_size = {params.get('pyramid_size', 4)};
pyramid_height = {params.get('pyramid_height', 2.5)};
pyramid_style = {params.get('pyramid_style', 4)};

total_height = base_thickness + outline_height;

// ===== PRE-CALCULATED VALID POSITIONS =====
// These positions have been verified to fit completely inside the SVG boundary
// Format: {"[x, y, rotation]" if has_rotation else "[x, y]"}
// Y-coordinates are flipped to match OpenSCAD's coordinate system
// rotation = angle in degrees for path-following orientation
valid_pyramid_positions = [
"""

    # Add all valid positions with Y-flip for OpenSCAD coordinate system
    for pos in valid_positions:
        x = pos[0]
        y_flipped = viewbox_height - pos[1]  # Flip Y coordinate based on SVG viewBox height
        if has_rotation:
            rotation = pos[2]
            scad_code += f"    [{x:.3f}, {y_flipped:.3f}, {rotation:.3f}],\n"
        else:
            scad_code += f"    [{x:.3f}, {y_flipped:.3f}],\n"

    scad_code += """];

// ===== MODULES =====

module scaled_svg() {
    // Note: OpenSCAD import() automatically handles SVG coordinate system
    // The imported SVG will match the OpenSCAD coordinate system
    // Use resize with both dimensions if height is specified, otherwise auto-scale
    resize([final_width, final_height > 0 ? final_height : 0, 0], auto=true) {
        import(svg_file);
    }
}

module base_shape_2d() {
    scaled_svg();
}

module outlined_shape_2d() {
    offset(r = outline_offset) {
        base_shape_2d();
    }
}

module grip_pyramid(path_rotation=0) {
    // Match the Python footprint calculation: 45° base + path_rotation
    // This ensures OpenSCAD pyramids match the debug visualization
    rotate([0, 0, 45 + path_rotation]) {
        cylinder(
            h = pyramid_height,
            r1 = pyramid_size / 1.414,
            r2 = 0,
            $fn = pyramid_style
        );
    }
}

// Place pyramids at pre-calculated positions
module pyramids_on_surface() {
    for (pos = valid_pyramid_positions) {
"""

    if has_rotation:
        scad_code += """        translate([pos[0], pos[1], base_thickness]) {
            grip_pyramid(path_rotation=pos[2]);
        }
"""
    else:
        scad_code += """        translate([pos[0], pos[1], base_thickness]) {
            grip_pyramid();
        }
"""

    scad_code += """    }
}

// ===== ASSEMBLY =====

module stomp_pad_complete() {
    union() {
        // Base layer
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

        // Pyramids at valid positions only
        pyramids_on_surface();
    }
}

stomp_pad_complete();
"""

    with open(output_file, 'w') as f:
        f.write(scad_code)

    _log(logger, f"Generated {output_file}")
    _log(logger, f"Valid pyramid positions: {len(valid_positions)}")


def _polygon_exterior_rings(geom):
    """Yield exterior coordinate rings for Polygon or MultiPolygon."""
    if geom.geom_type == 'Polygon':
        yield geom.exterior.xy
    elif geom.geom_type == 'MultiPolygon':
        for sub in geom.geoms:
            yield sub.exterior.xy


def save_debug_visualization(polygon, skeleton_points, valid_positions, output_file="debug_viz.svg", logger=None):
    """
    Save a debug SVG showing the polygon boundary, skeleton, and pyramid positions
    """
    try:
        import matplotlib.pyplot as plt
        from matplotlib.patches import Polygon as MplPolygon
        import matplotlib.patches as mpatches

        fig, ax = plt.subplots(figsize=(12, 10))

        # Plot polygon boundary (handles MultiPolygon from SVGs with multiple shapes)
        first = True
        for x, y in _polygon_exterior_rings(polygon):
            ax.plot(x, y, 'b-', linewidth=2,
                    label='Polygon Boundary' if first else None)
            first = False

        # Plot skeleton points
        if skeleton_points:
            skeleton_array = np.array(skeleton_points)
            ax.scatter(skeleton_array[:, 0], skeleton_array[:, 1],
                       c='green', s=1, alpha=0.5, label='Skeleton')

        # Plot pyramid positions
        if valid_positions:
            positions = np.array(valid_positions)
            ax.scatter(positions[:, 0], positions[:, 1],
                       c='red', s=20, marker='x', label='Pyramid Centers')

            # Draw pyramid footprints
            for pos in valid_positions:
                if len(pos) == 3:
                    x_pos, y_pos, rotation = pos
                    footprint = create_pyramid_footprint(x_pos, y_pos, 4, rotation)
                    fx, fy = footprint.exterior.xy
                    ax.plot(fx, fy, 'r-', linewidth=0.5, alpha=0.3)

        ax.set_aspect('equal')
        ax.legend()
        ax.grid(True, alpha=0.3)
        ax.set_title('Debug Visualization: Polygon, Skeleton, and Pyramids')

        plt.savefig(output_file, dpi=150, bbox_inches='tight')
        _log(logger, f"  Debug visualization saved: {output_file}")
        plt.close()

    except ImportError:
        _log(logger, "  matplotlib not available for debug visualization")


def main():
    """
    Example usage - configure parameters below
    """
    # ===== CONFIGURATION CONSTANTS =====
    # Input/Output files
    SVG_FILE = "-01.svg"
    OUTPUT_FILE = "stomp_pad_precalculated.scad"
    DEBUG_VIZ_FILE = "debug_viz.png"

    # SVG processing parameters
    TARGET_WIDTH = 152
    TARGET_HEIGHT = None  # Set to a value (e.g., 100) to constrain height, or None for width-only scaling
    SAMPLES_PER_SEGMENT = 40
    SKELETON_RESOLUTION = 0.4

    # Pyramid packing parameters
    PYRAMID_SIZE = 3
    PYRAMID_SPACING = 1.5
    SAFETY_MARGIN = 0.5  # Inset boundary to prevent pyramids from going over edge
    INCLUDE_ROTATION = True

    # OpenSCAD output parameters
    BASE_THICKNESS = 2.5
    OUTLINE_OFFSET = 2
    OUTLINE_HEIGHT = 2
    PYRAMID_HEIGHT = 3.5
    PYRAMID_STYLE = 4  # Number of sides (4 = square pyramid)

    # ===== PROCESSING =====
    # Parse SVG to polygon
    print(f"Parsing {SVG_FILE}...")
    polygon, svg_info = parse_svg_to_polygon(SVG_FILE, target_width=TARGET_WIDTH,
                                             target_height=TARGET_HEIGHT,
                                             samples_per_segment=SAMPLES_PER_SEGMENT)

    if polygon is None:
        print("ERROR: Could not parse SVG to polygon")
        return

    # Calculate skeleton for visualization
    print("Calculating skeleton...")
    skeleton_points = calculate_skeleton(polygon, resolution=SKELETON_RESOLUTION)

    # Calculate valid positions
    print("Calculating valid pyramid positions...")
    valid_positions = calculate_valid_pyramid_positions(
        polygon,
        pyramid_size=PYRAMID_SIZE,
        pyramid_spacing=PYRAMID_SPACING,
        target_width=TARGET_WIDTH,
        include_rotation=INCLUDE_ROTATION,
        safety_margin=SAFETY_MARGIN
    )

    # Save debug visualization
    print("Creating debug visualization...")
    save_debug_visualization(polygon, skeleton_points, valid_positions, DEBUG_VIZ_FILE)

    # Generate OpenSCAD file
    generate_openscad_with_positions(
        SVG_FILE,
        valid_positions,
        OUTPUT_FILE,
        svg_info=svg_info,
        target_width=TARGET_WIDTH,
        base_thickness=BASE_THICKNESS,
        outline_offset=OUTLINE_OFFSET,
        outline_height=OUTLINE_HEIGHT,
        pyramid_size=PYRAMID_SIZE,
        pyramid_height=PYRAMID_HEIGHT,
        pyramid_style=PYRAMID_STYLE
    )

    print(f"\n✓ Done! Open {OUTPUT_FILE} in OpenSCAD")
    print(f"✓ Check {DEBUG_VIZ_FILE} to verify polygon and pyramid alignment")


if __name__ == "__main__":
    main()
