# SVG to OpenSCAD/STL Bulk Processor

Automated bulk processing tool for converting SVG files to OpenSCAD files and STL models for stomp pad generation.

## Features

- **GUI Interface**: Easy-to-use graphical interface for selecting folders and configuring parameters
- **Bulk Processing**: Process multiple SVG files sequentially
- **Smart Caching**: Caches skeleton and position calculations for faster re-processing with different parameters
- **OpenSCAD Integration**: Automatically renders STL files using OpenSCAD headless mode
- **Progress Tracking**: Real-time progress bar and detailed logging

## Requirements

### Python Dependencies
Install required Python packages:
```bash
pip install -r requirements_bulk_processor.txt
```

### OpenSCAD
Download and install OpenSCAD for STL rendering:
- Windows: https://openscad.org/downloads.html
- The default installation path is usually `C:\Program Files\OpenSCAD\openscad.exe`

## Usage

### 1. Launch the Application
```bash
python bulk_processor_gui.py
```

### 2. Configure Files & Folders Tab

**Input Folder**: Select the folder containing your SVG files
- All `.svg` files in this folder will be processed

**Output Folder**: Select where to save generated OpenSCAD and STL files
- `.scad` files and `.stl` files will be saved here

**Cache Folder**: Select where to store cached data (default: `.cache`)
- Each SVG gets its own subfolder with:
  - Copy of the original SVG
  - `cache_data.json` with pre-calculated skeleton and positions

**OpenSCAD Executable**: Path to OpenSCAD executable
- Default: `openscad` (assumes it's in PATH)
- Windows example: `C:\Program Files\OpenSCAD\openscad.exe`

**Options**:
- ✓ **Generate STL files**: Enable to render STL files (requires OpenSCAD)
- ✓ **Use cache**: Enable to use cached data for faster re-processing

### 3. Configure Parameters Tab

Adjust these parameters to customize your stomp pads:

**SVG Processing**:
- **Target Width**: Final width of the stomp pad (mm)
- **Samples Per Segment**: Number of samples per curve segment (higher = more accurate)
- **Skeleton Resolution**: Resolution for skeleton calculation (lower = more points)

**Pyramid Packing**:
- **Pyramid Size**: Base size of each pyramid
- **Pyramid Spacing**: Spacing between pyramids
- **Safety Margin**: Inset from boundary edge
- **Include Rotation**: Align pyramids with path direction

**OpenSCAD Output**:
- **Base Thickness**: Thickness of base layer
- **Outline Offset**: Width of raised outline border
- **Outline Height**: Height of raised outline border
- **Pyramid Height**: Height of grip pyramids
- **Pyramid Style ($fn)**: Number of sides (4 = square pyramid)

### 4. Start Processing

Switch to the **Processing** tab and click **Start Processing**

The application will:
1. Find all SVG files in the input folder
2. For each SVG:
   - Check if cached data exists (if cache enabled)
   - Parse SVG or load from cache
   - Calculate skeleton and pyramid positions (or use cached)
   - Generate OpenSCAD file
   - Render STL file (if enabled)
3. Show progress and detailed logs

## Cache System

### How Caching Works

When processing an SVG file `mydesign.svg`:

1. **First Run** (no cache):
   - Parse SVG, calculate skeleton, find pyramid positions
   - Save results to `.cache/mydesign/cache_data.json`
   - Copy SVG to `.cache/mydesign/mydesign.svg`

2. **Subsequent Runs** (with cache):
   - Load skeleton and positions from cache (fast!)
   - Skip expensive calculations
   - Generate new OpenSCAD with updated parameters

### When Cache is Used

Cache is only used if:
- "Use cache" option is enabled
- Cache folder contains `cache_data.json` for the SVG
- Cache file is valid JSON

### When to Clear Cache

Clear cache (delete `.cache` folder) when:
- You modify the original SVG file
- You change SVG processing parameters:
  - Target Width
  - Samples Per Segment
  - Skeleton Resolution
  - Pyramid Size
  - Pyramid Spacing
  - Safety Margin

### What Can Be Changed Without Clearing Cache

These parameters can be changed without clearing cache:
- All OpenSCAD output parameters (thickness, heights, styles)
- Generate STL option
- Output folder location

## Output Structure

### Output Folder
```
output/
├── design1.scad
├── design1.stl
├── design2.scad
├── design2.stl
└── ...
```

### Cache Folder
```
.cache/
├── design1/
│   ├── design1.svg      (copy of original)
│   └── cache_data.json  (skeleton + positions)
├── design2/
│   ├── design2.svg
│   └── cache_data.json
└── ...
```

## Troubleshooting

### "OpenSCAD executable not found"
- Make sure OpenSCAD is installed
- Browse to the correct OpenSCAD executable path in Files & Folders tab
- On Windows, typically: `C:\Program Files\OpenSCAD\openscad.exe`

### "STL rendering failed"
- Check OpenSCAD path is correct
- Open the generated `.scad` file manually in OpenSCAD to see errors
- Check the processing log for error messages

### "Cache invalid, reprocessing"
- Cache file is corrupted or outdated
- File will be automatically reprocessed
- Cache will be regenerated

### Slow Processing
- First run is slower (calculating skeletons)
- Enable caching for subsequent runs
- Reduce "Samples Per Segment" for faster (but less accurate) processing
- Increase "Skeleton Resolution" for faster skeleton calculation

## Sequential Processing

The tool processes files **sequentially** (one at a time) to:
- Ensure stable processing
- Provide clear progress tracking
- Avoid resource conflicts

For large batches, enable caching to speed up parameter adjustments.

## Tips

1. **Test First**: Process 1-2 SVG files first to verify parameters
2. **Use Cache**: Always enable cache for production runs
3. **Batch Similar Files**: Process similar designs together with same parameters
4. **Save Parameters**: Note your parameter settings for reproducibility
5. **Check Logs**: Review the processing log for warnings or errors

## Files Generated

For each SVG input file, you get:

- **`.scad` file**: OpenSCAD source with pre-calculated pyramid positions
- **`.stl` file**: 3D printable model (if STL generation enabled)
- **Debug visualization**: Not generated in bulk mode (only in single-file mode)

## Integration with Single-File Tool

You can still use `pyramid_position_calculator.py` for single-file processing with debug visualization:

```bash
python pyramid_position_calculator.py
```

This generates:
- `stomp_pad_precalculated.scad`
- `debug_viz.png` (shows boundary, skeleton, and pyramid placement)
