#!/usr/bin/env python3
"""
Bulk SVG to OpenSCAD/STL Processor with GUI
Processes multiple SVG files, generates OpenSCAD files, and renders STLs
Uses caching for faster re-processing with different parameters
"""

import tkinter as tk
from tkinter import ttk, filedialog, scrolledtext
import os
import sys
import json
import subprocess
import shutil
from pathlib import Path
from datetime import datetime
import threading

# Import the pyramid calculator functions
from pyramid_position_calculator import (
    parse_svg_to_polygon,
    calculate_skeleton,
    calculate_valid_pyramid_positions,
    generate_openscad_with_positions,
    save_debug_visualization
)


class BulkProcessorGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("SVG to OpenSCAD/STL Bulk Processor")
        self.root.geometry("900x800")

        # Config file path
        self.config_file = Path.home() / ".stomp_pad_processor_config.json"

        # Variables
        self.input_folder = tk.StringVar()
        self.output_folder = tk.StringVar()
        self.cache_folder = tk.StringVar(value=".cache")
        self.openscad_path = tk.StringVar(value="openscad")

        # Parameters (matching pyramid_position_calculator.py)
        self.target_width = tk.DoubleVar(value=152)
        self.samples_per_segment = tk.IntVar(value=40)
        self.skeleton_resolution = tk.DoubleVar(value=0.4)

        self.pyramid_size = tk.DoubleVar(value=3)
        self.pyramid_spacing = tk.DoubleVar(value=1.5)
        self.safety_margin = tk.DoubleVar(value=0.5)
        self.include_rotation = tk.BooleanVar(value=True)

        self.base_thickness = tk.DoubleVar(value=2.5)
        self.outline_offset = tk.DoubleVar(value=2)
        self.outline_height = tk.DoubleVar(value=2)
        self.pyramid_height = tk.DoubleVar(value=3.5)
        self.pyramid_style = tk.IntVar(value=4)

        self.generate_stl = tk.BooleanVar(value=True)
        self.use_cache = tk.BooleanVar(value=True)

        # Processing control
        self.stop_requested = False
        self.is_processing = False

        # Load saved configuration
        self.load_config()

        self.setup_ui()

        # Save config on window close
        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)

    def setup_ui(self):
        # Create notebook for tabs
        notebook = ttk.Notebook(self.root)
        notebook.pack(fill='both', expand=True, padx=10, pady=10)

        # Tab 1: File Selection
        files_frame = ttk.Frame(notebook)
        notebook.add(files_frame, text="Files & Folders")
        self.setup_files_tab(files_frame)

        # Tab 2: Parameters
        params_frame = ttk.Frame(notebook)
        notebook.add(params_frame, text="Parameters")
        self.setup_parameters_tab(params_frame)

        # Tab 3: Processing
        process_frame = ttk.Frame(notebook)
        notebook.add(process_frame, text="Processing")
        self.setup_processing_tab(process_frame)

    def setup_files_tab(self, parent):
        # Input folder
        ttk.Label(parent, text="Input Folder (SVG files):").grid(row=0, column=0, sticky='w', padx=5, pady=5)
        ttk.Entry(parent, textvariable=self.input_folder, width=50).grid(row=0, column=1, padx=5, pady=5)
        ttk.Button(parent, text="Browse...", command=self.browse_input_folder).grid(row=0, column=2, padx=5, pady=5)

        # Output folder
        ttk.Label(parent, text="Output Folder:").grid(row=1, column=0, sticky='w', padx=5, pady=5)
        ttk.Entry(parent, textvariable=self.output_folder, width=50).grid(row=1, column=1, padx=5, pady=5)
        ttk.Button(parent, text="Browse...", command=self.browse_output_folder).grid(row=1, column=2, padx=5, pady=5)

        # Cache folder
        ttk.Label(parent, text="Cache Folder:").grid(row=2, column=0, sticky='w', padx=5, pady=5)
        ttk.Entry(parent, textvariable=self.cache_folder, width=50).grid(row=2, column=1, padx=5, pady=5)
        ttk.Button(parent, text="Browse...", command=self.browse_cache_folder).grid(row=2, column=2, padx=5, pady=5)

        # OpenSCAD path
        ttk.Label(parent, text="OpenSCAD Executable:").grid(row=3, column=0, sticky='w', padx=5, pady=5)
        ttk.Entry(parent, textvariable=self.openscad_path, width=50).grid(row=3, column=1, padx=5, pady=5)
        ttk.Button(parent, text="Browse...", command=self.browse_openscad).grid(row=3, column=2, padx=5, pady=5)

        # Options
        ttk.Checkbutton(parent, text="Generate STL files (requires OpenSCAD)",
                       variable=self.generate_stl).grid(row=4, column=1, sticky='w', padx=5, pady=5)
        ttk.Checkbutton(parent, text="Use cache for faster processing",
                       variable=self.use_cache).grid(row=5, column=1, sticky='w', padx=5, pady=5)

        # Info label
        info_text = "Select the folder containing SVG files and where to output OpenSCAD/STL files.\n" \
                   "Cache folder stores pre-calculated data for faster re-processing."
        ttk.Label(parent, text=info_text, wraplength=700, foreground='gray').grid(
            row=6, column=0, columnspan=3, sticky='w', padx=5, pady=20)

    def setup_parameters_tab(self, parent):
        # SVG Processing Parameters
        group1 = ttk.LabelFrame(parent, text="SVG Processing", padding=10)
        group1.grid(row=0, column=0, sticky='ew', padx=10, pady=5)

        ttk.Label(group1, text="Target Width:").grid(row=0, column=0, sticky='w')
        ttk.Entry(group1, textvariable=self.target_width, width=15).grid(row=0, column=1, padx=5)

        ttk.Label(group1, text="Samples Per Segment:").grid(row=1, column=0, sticky='w')
        ttk.Entry(group1, textvariable=self.samples_per_segment, width=15).grid(row=1, column=1, padx=5)

        ttk.Label(group1, text="Skeleton Resolution:").grid(row=2, column=0, sticky='w')
        ttk.Entry(group1, textvariable=self.skeleton_resolution, width=15).grid(row=2, column=1, padx=5)

        # Pyramid Packing Parameters
        group2 = ttk.LabelFrame(parent, text="Pyramid Packing", padding=10)
        group2.grid(row=1, column=0, sticky='ew', padx=10, pady=5)

        ttk.Label(group2, text="Pyramid Size:").grid(row=0, column=0, sticky='w')
        ttk.Entry(group2, textvariable=self.pyramid_size, width=15).grid(row=0, column=1, padx=5)

        ttk.Label(group2, text="Pyramid Spacing:").grid(row=1, column=0, sticky='w')
        ttk.Entry(group2, textvariable=self.pyramid_spacing, width=15).grid(row=1, column=1, padx=5)

        ttk.Label(group2, text="Safety Margin:").grid(row=2, column=0, sticky='w')
        ttk.Entry(group2, textvariable=self.safety_margin, width=15).grid(row=2, column=1, padx=5)

        ttk.Checkbutton(group2, text="Include Rotation",
                       variable=self.include_rotation).grid(row=3, column=0, columnspan=2, sticky='w')

        # OpenSCAD Output Parameters
        group3 = ttk.LabelFrame(parent, text="OpenSCAD Output", padding=10)
        group3.grid(row=2, column=0, sticky='ew', padx=10, pady=5)

        ttk.Label(group3, text="Base Thickness:").grid(row=0, column=0, sticky='w')
        ttk.Entry(group3, textvariable=self.base_thickness, width=15).grid(row=0, column=1, padx=5)

        ttk.Label(group3, text="Outline Offset:").grid(row=1, column=0, sticky='w')
        ttk.Entry(group3, textvariable=self.outline_offset, width=15).grid(row=1, column=1, padx=5)

        ttk.Label(group3, text="Outline Height:").grid(row=2, column=0, sticky='w')
        ttk.Entry(group3, textvariable=self.outline_height, width=15).grid(row=2, column=1, padx=5)

        ttk.Label(group3, text="Pyramid Height:").grid(row=3, column=0, sticky='w')
        ttk.Entry(group3, textvariable=self.pyramid_height, width=15).grid(row=3, column=1, padx=5)

        ttk.Label(group3, text="Pyramid Style ($fn):").grid(row=4, column=0, sticky='w')
        ttk.Entry(group3, textvariable=self.pyramid_style, width=15).grid(row=4, column=1, padx=5)

    def setup_processing_tab(self, parent):
        # Progress bar
        ttk.Label(parent, text="Processing Progress:").pack(anchor='w', padx=10, pady=5)
        self.progress_var = tk.DoubleVar()
        self.progress_bar = ttk.Progressbar(parent, variable=self.progress_var, maximum=100)
        self.progress_bar.pack(fill='x', padx=10, pady=5)

        # Status label
        self.status_label = ttk.Label(parent, text="Ready", foreground='green')
        self.status_label.pack(anchor='w', padx=10, pady=5)

        # Log output
        ttk.Label(parent, text="Processing Log:").pack(anchor='w', padx=10, pady=5)
        self.log_text = scrolledtext.ScrolledText(parent, height=25, width=100)
        self.log_text.pack(fill='both', expand=True, padx=10, pady=5)

        # Buttons
        button_frame = ttk.Frame(parent)
        button_frame.pack(fill='x', padx=10, pady=10)

        self.start_button = ttk.Button(button_frame, text="Start Processing",
                                       command=self.start_processing)
        self.start_button.pack(side='left', padx=5)

        self.stop_button = ttk.Button(button_frame, text="Stop Processing",
                                      command=self.stop_processing,
                                      state='disabled')
        self.stop_button.pack(side='left', padx=5)

        ttk.Button(button_frame, text="Clear Log",
                  command=lambda: self.log_text.delete(1.0, tk.END)).pack(side='left', padx=5)

    def browse_input_folder(self):
        folder = filedialog.askdirectory(title="Select Input Folder (SVG files)")
        if folder:
            self.input_folder.set(folder)

    def browse_output_folder(self):
        folder = filedialog.askdirectory(title="Select Output Folder")
        if folder:
            self.output_folder.set(folder)

    def browse_cache_folder(self):
        folder = filedialog.askdirectory(title="Select Cache Folder")
        if folder:
            self.cache_folder.set(folder)

    def browse_openscad(self):
        file = filedialog.askopenfilename(title="Select OpenSCAD Executable",
                                         filetypes=[("Executable", "*.exe"), ("All files", "*.*")])
        if file:
            self.openscad_path.set(file)

    def log(self, message):
        """Add message to log with timestamp (thread-safe)"""
        timestamp = datetime.now().strftime("%H:%M:%S")
        msg = f"[{timestamp}] {message}\n"
        # Schedule GUI update in main thread
        self.root.after(0, lambda: self._update_log(msg))

    def _update_log(self, message):
        """Internal method to update log (must be called from main thread)"""
        self.log_text.insert(tk.END, message)
        self.log_text.see(tk.END)

    def update_status(self, message, color='black'):
        """Update status label (thread-safe)"""
        # Schedule GUI update in main thread
        self.root.after(0, lambda: self._update_status_label(message, color))

    def _update_status_label(self, message, color):
        """Internal method to update status (must be called from main thread)"""
        self.status_label.config(text=message, foreground=color)

    def start_processing(self):
        """Start the bulk processing in a background thread"""
        # Validate inputs
        if not self.input_folder.get():
            self.log("ERROR: Please select an input folder")
            return

        if not self.output_folder.get():
            self.log("ERROR: Please select an output folder")
            return

        # Reset stop flag
        self.stop_requested = False
        self.is_processing = True

        # Update button states
        self.start_button.config(state='disabled')
        self.stop_button.config(state='normal')

        # Run processing in a separate thread to keep GUI responsive
        processing_thread = threading.Thread(target=self._run_processing, daemon=True)
        processing_thread.start()

    def _run_processing(self):
        """Internal method to run processing in background thread"""
        try:
            self.process_all_svgs()
        except Exception as e:
            self.log(f"ERROR: {str(e)}")
            self.update_status("Error occurred", 'red')
        finally:
            self.is_processing = False
            # Use root.after to safely update GUI from thread
            self.root.after(0, self._reset_buttons)

    def _reset_buttons(self):
        """Reset button states (called from main thread)"""
        self.start_button.config(state='normal')
        self.stop_button.config(state='disabled')

    def stop_processing(self):
        """Request to stop the current processing"""
        if self.is_processing:
            self.stop_requested = True
            self.log("\n" + "="*60)
            self.log("STOP REQUESTED - Finishing current file and stopping...")
            self.log("="*60)
            self.update_status("Stopping...", 'orange')
            self.stop_button.config(state='disabled')

    def process_all_svgs(self):
        """Process all SVG files in the input folder"""
        input_path = Path(self.input_folder.get())
        output_path = Path(self.output_folder.get())
        cache_path = Path(self.cache_folder.get())

        # Create output and cache directories
        output_path.mkdir(parents=True, exist_ok=True)
        cache_path.mkdir(parents=True, exist_ok=True)

        # Find all SVG files
        svg_files = list(input_path.glob("*.svg"))

        if not svg_files:
            self.log("WARNING: No SVG files found in input folder")
            self.update_status("No files to process", 'orange')
            return

        self.log(f"Found {len(svg_files)} SVG file(s)")
        self.update_status(f"Processing {len(svg_files)} files...", 'blue')

        # Process each SVG file
        files_processed = 0
        for i, svg_file in enumerate(svg_files):
            # Check if stop was requested
            if self.stop_requested:
                self.log(f"\n{'='*60}")
                self.log(f"Processing stopped by user")
                self.log(f"Processed {files_processed}/{len(svg_files)} files")
                self.log(f"{'='*60}")
                self.update_status(f"Stopped ({files_processed}/{len(svg_files)} completed)", 'orange')
                return

            progress = (i / len(svg_files)) * 100
            # Thread-safe progress bar update
            self.root.after(0, lambda p=progress: self.progress_var.set(p))

            self.log(f"\n{'='*60}")
            self.log(f"Processing {i+1}/{len(svg_files)}: {svg_file.name}")
            self.log(f"{'='*60}")

            try:
                self.process_single_svg(svg_file, output_path, cache_path)
                files_processed += 1
            except Exception as e:
                self.log(f"ERROR processing {svg_file.name}: {str(e)}")
                continue

        # Thread-safe final progress update
        self.root.after(0, lambda: self.progress_var.set(100))
        self.log(f"\n{'='*60}")
        self.log("Processing complete!")
        self.log(f"Processed {files_processed}/{len(svg_files)} files successfully")
        self.log(f"{'='*60}")
        self.update_status("Complete!", 'green')

    def process_single_svg(self, svg_file, output_path, cache_path):
        """Process a single SVG file"""
        file_stem = svg_file.stem

        # Create cache subfolder for this file
        file_cache_path = cache_path / file_stem
        file_cache_path.mkdir(parents=True, exist_ok=True)

        # Cache file paths
        cache_svg = file_cache_path / svg_file.name
        cache_data = file_cache_path / "cache_data.json"

        # Check if we can use cached data
        use_cached = False
        if self.use_cache.get() and cache_data.exists():
            self.log(f"  Found cached data for {file_stem}")
            try:
                cached_result = self.load_from_cache(cache_data)
                use_cached = True
                self.log(f"  Using cached skeleton and positions")
            except Exception as e:
                self.log(f"  Cache invalid, reprocessing: {str(e)}")
                use_cached = False

        if not use_cached:
            # Parse SVG and calculate positions
            self.log(f"  Parsing SVG...")
            polygon, svg_info = parse_svg_to_polygon(
                str(svg_file),
                target_width=self.target_width.get(),
                samples_per_segment=self.samples_per_segment.get()
            )

            self.log(f"  Calculating skeleton...")
            skeleton_points = calculate_skeleton(
                polygon,
                resolution=self.skeleton_resolution.get()
            )

            self.log(f"  Calculating pyramid positions...")
            valid_positions = calculate_valid_pyramid_positions(
                polygon,
                pyramid_size=self.pyramid_size.get(),
                pyramid_spacing=self.pyramid_spacing.get(),
                target_width=self.target_width.get(),
                include_rotation=self.include_rotation.get(),
                safety_margin=self.safety_margin.get()
            )

            # Save to cache
            self.log(f"  Saving to cache...")
            self.save_to_cache(cache_data, polygon, svg_info, skeleton_points, valid_positions)

            # Copy SVG to cache folder
            shutil.copy2(svg_file, cache_svg)
        else:
            # Use cached data
            polygon = cached_result['polygon']
            svg_info = cached_result['svg_info']
            skeleton_points = cached_result['skeleton_points']
            valid_positions = cached_result['valid_positions']

        # Copy SVG to output folder so OpenSCAD can find it
        output_svg = output_path / svg_file.name
        if cache_svg.exists():
            shutil.copy2(cache_svg, output_svg)
        else:
            shutil.copy2(svg_file, output_svg)

        # Generate OpenSCAD file
        self.log(f"  Generating OpenSCAD file...")
        scad_output = output_path / f"{file_stem}.scad"

        # Use just the filename (not full path) so OpenSCAD looks in same directory
        generate_openscad_with_positions(
            svg_file.name,  # Just the filename, not full path
            valid_positions,
            str(scad_output),
            svg_info=svg_info,
            target_width=self.target_width.get(),
            base_thickness=self.base_thickness.get(),
            outline_offset=self.outline_offset.get(),
            outline_height=self.outline_height.get(),
            pyramid_size=self.pyramid_size.get(),
            pyramid_height=self.pyramid_height.get(),
            pyramid_style=self.pyramid_style.get()
        )

        self.log(f"  ✓ OpenSCAD file: {scad_output.name}")
        self.log(f"  ✓ SVG copied to output: {output_svg.name}")

        # Generate STL if requested
        if self.generate_stl.get():
            self.log(f"  Rendering STL...")
            stl_output = output_path / f"{file_stem}.stl"

            success = self.render_stl(scad_output, stl_output)
            if success:
                self.log(f"  ✓ STL file: {stl_output.name}")
            else:
                self.log(f"  ✗ STL rendering failed")

    def save_to_cache(self, cache_file, polygon, svg_info, skeleton_points, valid_positions):
        """Save processed data to cache file"""
        from shapely.geometry import mapping

        cache_data = {
            'polygon': mapping(polygon),
            'svg_info': svg_info,
            'skeleton_points': skeleton_points,
            'valid_positions': valid_positions,
            'timestamp': datetime.now().isoformat()
        }

        with open(cache_file, 'w') as f:
            json.dump(cache_data, f, indent=2)

    def load_from_cache(self, cache_file):
        """Load processed data from cache file"""
        from shapely.geometry import shape

        with open(cache_file, 'r') as f:
            cache_data = json.load(f)

        return {
            'polygon': shape(cache_data['polygon']),
            'svg_info': cache_data['svg_info'],
            'skeleton_points': cache_data['skeleton_points'],
            'valid_positions': cache_data['valid_positions']
        }

    def render_stl(self, scad_file, stl_file):
        """Render OpenSCAD file to STL using headless mode"""
        try:
            cmd = [
                self.openscad_path.get(),
                "-o", str(stl_file),
                str(scad_file)
            ]

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=300  # 5 minute timeout
            )

            if result.returncode == 0:
                return True
            else:
                self.log(f"    OpenSCAD error: {result.stderr}")
                return False

        except subprocess.TimeoutExpired:
            self.log(f"    Timeout rendering STL")
            return False
        except FileNotFoundError:
            self.log(f"    OpenSCAD executable not found: {self.openscad_path.get()}")
            return False
        except Exception as e:
            self.log(f"    Error: {str(e)}")
            return False

    def save_config(self):
        """Save current settings to config file"""
        config = {
            # Paths
            'input_folder': self.input_folder.get(),
            'output_folder': self.output_folder.get(),
            'cache_folder': self.cache_folder.get(),
            'openscad_path': self.openscad_path.get(),

            # SVG Processing
            'target_width': self.target_width.get(),
            'samples_per_segment': self.samples_per_segment.get(),
            'skeleton_resolution': self.skeleton_resolution.get(),

            # Pyramid Packing
            'pyramid_size': self.pyramid_size.get(),
            'pyramid_spacing': self.pyramid_spacing.get(),
            'safety_margin': self.safety_margin.get(),
            'include_rotation': self.include_rotation.get(),

            # OpenSCAD Output
            'base_thickness': self.base_thickness.get(),
            'outline_offset': self.outline_offset.get(),
            'outline_height': self.outline_height.get(),
            'pyramid_height': self.pyramid_height.get(),
            'pyramid_style': self.pyramid_style.get(),

            # Options
            'generate_stl': self.generate_stl.get(),
            'use_cache': self.use_cache.get()
        }

        try:
            with open(self.config_file, 'w') as f:
                json.dump(config, f, indent=2)
        except Exception as e:
            print(f"Warning: Could not save config: {e}")

    def load_config(self):
        """Load settings from config file if it exists"""
        if not self.config_file.exists():
            return

        try:
            with open(self.config_file, 'r') as f:
                config = json.load(f)

            # Load paths
            if 'input_folder' in config:
                self.input_folder.set(config['input_folder'])
            if 'output_folder' in config:
                self.output_folder.set(config['output_folder'])
            if 'cache_folder' in config:
                self.cache_folder.set(config['cache_folder'])
            if 'openscad_path' in config:
                self.openscad_path.set(config['openscad_path'])

            # Load SVG Processing parameters
            if 'target_width' in config:
                self.target_width.set(config['target_width'])
            if 'samples_per_segment' in config:
                self.samples_per_segment.set(config['samples_per_segment'])
            if 'skeleton_resolution' in config:
                self.skeleton_resolution.set(config['skeleton_resolution'])

            # Load Pyramid Packing parameters
            if 'pyramid_size' in config:
                self.pyramid_size.set(config['pyramid_size'])
            if 'pyramid_spacing' in config:
                self.pyramid_spacing.set(config['pyramid_spacing'])
            if 'safety_margin' in config:
                self.safety_margin.set(config['safety_margin'])
            if 'include_rotation' in config:
                self.include_rotation.set(config['include_rotation'])

            # Load OpenSCAD Output parameters
            if 'base_thickness' in config:
                self.base_thickness.set(config['base_thickness'])
            if 'outline_offset' in config:
                self.outline_offset.set(config['outline_offset'])
            if 'outline_height' in config:
                self.outline_height.set(config['outline_height'])
            if 'pyramid_height' in config:
                self.pyramid_height.set(config['pyramid_height'])
            if 'pyramid_style' in config:
                self.pyramid_style.set(config['pyramid_style'])

            # Load options
            if 'generate_stl' in config:
                self.generate_stl.set(config['generate_stl'])
            if 'use_cache' in config:
                self.use_cache.set(config['use_cache'])

        except Exception as e:
            print(f"Warning: Could not load config: {e}")

    def on_closing(self):
        """Save config and close application"""
        self.save_config()
        self.root.destroy()


def main():
    root = tk.Tk()
    app = BulkProcessorGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
