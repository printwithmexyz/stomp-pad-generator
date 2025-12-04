#!/usr/bin/env python3
"""
SVG-Based Pyramid Position Calculator
Pre-calculates which pyramid positions fit inside the SVG boundary
Uses centerline/skeleton-based hexagonal packing
Generates OpenSCAD code with ONLY valid positions
"""

import numpy as np
from shapely.geometry import Point, Polygon, LineString, MultiPoint
from shapely.ops import unary_union, voronoi_diagram
from svg.path import parse_path
import xml.etree.ElementTree as ET
from pathlib import Path
from scipy.spatial import distance
from skimage.morphology import medial_axis, skeletonize


def parse_svg_to_polygon(svg_file, target_width=100, samples_per_segment=20):
    """
    Parse SVG file and convert to Shapely polygon
    Returns (polygon, svg_viewbox_info) where polygon is scaled to target_width
    Properly samples curves and ensures closed paths
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

    # Extract and properly sample all paths
    all_points = []

    for path_elem in root.findall('.//{http://www.w3.org/2000/svg}path'):
        d = path_elem.get('d')
        if not d:
            continue

        path = parse_path(d)

        # Sample each segment of the path to capture curves
        path_points = []
        for segment in path:
            # Sample multiple points along each segment
            for i in range(samples_per_segment):
                t = i / samples_per_segment
                point = segment.point(t)
                path_points.append((point.real, point.imag))

        # Add the final point of the last segment
        if path:
            final_point = path[-1].end
            path_points.append((final_point.real, final_point.imag))

        # Check if path is closed (first and last points are close)
        if len(path_points) > 2:
            first = np.array(path_points[0])
            last = np.array(path_points[-1])
            distance = np.linalg.norm(first - last)

            # If not closed, force closure
            if distance > 0.1:  # threshold for "close enough"
                path_points.append(path_points[0])

        all_points.extend(path_points)

    if len(all_points) < 3:
        print(f"ERROR: Not enough points parsed from SVG ({len(all_points)} points)")
        return None

    # Create polygon from sampled points
    poly = Polygon(all_points)

    if not poly.is_valid:
        # Try to fix invalid polygon
        poly = poly.buffer(0)
        if not poly.is_valid:
            print("ERROR: Could not create valid polygon from SVG")
            return None

    # Get original bounds
    bounds = poly.bounds
    original_min_x, original_min_y, original_max_x, original_max_y = bounds
    original_width = original_max_x - original_min_x
    original_height = original_max_y - original_min_y

    from shapely.affinity import scale as shapely_scale

    # Scale to target width while maintaining aspect ratio
    # IMPORTANT: Scale from origin (0,0) to match OpenSCAD's resize() behavior
    # OpenSCAD resize() scales the SVG but keeps it in its original coordinate space
    # We do NOT translate to origin - we keep the SVG viewBox coordinates
    scale_factor = target_width / original_width
    poly_scaled = shapely_scale(poly, xfact=scale_factor, yfact=scale_factor, origin=(0, 0))

    bounds_scaled = poly_scaled.bounds

    print(f"  SVG parsed: {len(all_points)} points sampled")
    print(f"  Original size: {original_width:.1f} x {original_height:.1f}")
    print(
        f"  Scaled bounds: ({bounds_scaled[0]:.1f}, {bounds_scaled[1]:.1f}) to ({bounds_scaled[2]:.1f}, {bounds_scaled[3]:.1f})")
    print(f"  Scaled size: {bounds_scaled[2] - bounds_scaled[0]:.1f} x {bounds_scaled[3] - bounds_scaled[1]:.1f}")

    # Return polygon and SVG viewBox info (scaled)
    svg_info = {
        'viewbox_width': svg_width * scale_factor if svg_width else bounds_scaled[2] - bounds_scaled[0],
        'viewbox_height': svg_height * scale_factor if svg_height else bounds_scaled[3] - bounds_scaled[1],
        'scale_factor': scale_factor
    }

    return poly_scaled, svg_info


def calculate_skeleton(polygon, resolution=0.5):
    """
    Calculate the centerline/skeleton of the polygon using medial axis transform
    Returns a list of skeleton points along the centerline
    """
    bounds = polygon.bounds
    min_x, min_y, max_x, max_y = bounds

    # Create a rasterized version of the polygon
    width = int((max_x - min_x) / resolution) + 1
    height = int((max_y - min_y) / resolution) + 1

    # Create binary image
    binary_image = np.zeros((height, width), dtype=bool)

    for i in range(height):
        for j in range(width):
            x = min_x + j * resolution
            y = min_y + i * resolution
            if polygon.contains(Point(x, y)):
                binary_image[i, j] = True

    # Calculate medial axis (skeleton)
    skeleton = medial_axis(binary_image)

    # Extract skeleton points
    skeleton_points = []
    for i in range(height):
        for j in range(width):
            if skeleton[i, j]:
                x = min_x + j * resolution
                y = min_y + i * resolution
                skeleton_points.append((x, y))

    return skeleton_points


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

    # Use PCA to find principal direction
    centered = nearby_points - nearby_points.mean(axis=0)
    cov = np.cov(centered.T)
    eigenvalues, eigenvectors = np.linalg.eig(cov)

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
        safety_margin=0.5
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
    print("  Calculating skeleton/centerline...")
    # Calculate the skeleton/centerline of the shape
    skeleton_points = calculate_skeleton(polygon, resolution=0.3)

    if not skeleton_points:
        print("  WARNING: No skeleton points found")
        return []

    print(f"  Found {len(skeleton_points)} skeleton points")

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

    print(f"  Testing {num_rows * num_cols} potential positions...")

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

    print(f"  Valid positions: {len(valid_positions)}")
    return valid_positions


def generate_openscad_with_positions(
        svg_file,
        valid_positions,
        output_file,
        svg_info=None,
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
target_width = {params.get('target_width', 100)};
maintain_aspect_ratio = true;

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
    resize([target_width, 0, 0], auto=maintain_aspect_ratio) {
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

    print(f"Generated {output_file}")
    print(f"Valid pyramid positions: {len(valid_positions)}")


def save_debug_visualization(polygon, skeleton_points, valid_positions, output_file="debug_viz.svg"):
    """
    Save a debug SVG showing the polygon boundary, skeleton, and pyramid positions
    """
    try:
        import matplotlib.pyplot as plt
        from matplotlib.patches import Polygon as MplPolygon
        import matplotlib.patches as mpatches

        fig, ax = plt.subplots(figsize=(12, 10))

        # Plot polygon boundary
        x, y = polygon.exterior.xy
        ax.plot(x, y, 'b-', linewidth=2, label='Polygon Boundary')

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
        print(f"  Debug visualization saved: {output_file}")
        plt.close()

    except ImportError:
        print("  matplotlib not available for debug visualization")


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
