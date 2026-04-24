#!/usr/bin/env python3
"""
Bulk SVG to OpenSCAD/STL Processor with GUI
Processes multiple SVG files, generates OpenSCAD files, and renders STLs
Uses caching for faster re-processing with different parameters
"""

import tkinter as tk
from tkinter import ttk, filedialog, scrolledtext
import json
import queue
import subprocess
import shutil
import multiprocessing
from pathlib import Path
from datetime import datetime
import threading
from concurrent.futures import ThreadPoolExecutor, ProcessPoolExecutor, as_completed

# Import the pyramid calculator functions
from pyramid_position_calculator import (
    parse_svg_to_polygon,
    calculate_skeleton,
    calculate_valid_pyramid_positions,
    generate_openscad_with_positions,
    save_debug_visualization
)


# ===== Module-level helpers (must be top-level for ProcessPoolExecutor pickling) =====

def _save_cache(cache_file, polygon, svg_info, skeleton_points, valid_positions):
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


def _load_cache(cache_file):
    from shapely.geometry import shape
    with open(cache_file, 'r') as f:
        cache_data = json.load(f)
    return {
        'polygon': shape(cache_data['polygon']),
        'svg_info': cache_data['svg_info'],
        'skeleton_points': cache_data['skeleton_points'],
        'valid_positions': cache_data['valid_positions']
    }


def _svg_worker(svg_file_path, output_path_str, cache_path_str, params, use_cache, log_queue):
    """Process pool worker: SVG -> .scad. Returns dict with status and scad path.

    Logs are pushed to log_queue (a multiprocessing Manager queue) prefixed with [filename].
    Caller is responsible for STL rendering — that runs in a separate thread pool.
    """
    svg_file = Path(svg_file_path)
    output_path = Path(output_path_str)
    cache_path = Path(cache_path_str)
    file_stem = svg_file.stem

    def log(msg):
        log_queue.put(f"[{file_stem}] {msg}")

    try:
        file_cache_path = cache_path / file_stem
        file_cache_path.mkdir(parents=True, exist_ok=True)
        cache_svg = file_cache_path / svg_file.name
        cache_data = file_cache_path / "cache_data.json"

        cached_result = None
        if use_cache and cache_data.exists():
            log("Found cached data")
            try:
                cached_result = _load_cache(cache_data)
                log("Using cached skeleton and positions")
            except Exception as e:
                log(f"Cache invalid, reprocessing: {e}")
                cached_result = None

        if cached_result is None:
            log("Parsing SVG...")
            polygon, svg_info = parse_svg_to_polygon(
                str(svg_file),
                target_width=params['target_width'],
                target_height=params['target_height'] if params['target_height'] > 0 else None,
                samples_per_segment=params['samples_per_segment'],
                logger=log
            )
            log("Calculating skeleton...")
            skeleton_points = calculate_skeleton(polygon, resolution=params['skeleton_resolution'])
            log("Calculating pyramid positions...")
            valid_positions = calculate_valid_pyramid_positions(
                polygon,
                pyramid_size=params['pyramid_size'],
                pyramid_spacing=params['pyramid_spacing'],
                target_width=params['target_width'],
                include_rotation=params['include_rotation'],
                safety_margin=params['safety_margin'],
                skeleton_points=skeleton_points,
                logger=log
            )
            log("Saving to cache...")
            _save_cache(cache_data, polygon, svg_info, skeleton_points, valid_positions)
            shutil.copy2(svg_file, cache_svg)
        else:
            polygon = cached_result['polygon']
            svg_info = cached_result['svg_info']
            valid_positions = cached_result['valid_positions']

        output_svg = output_path / svg_file.name
        if cache_svg.exists():
            shutil.copy2(cache_svg, output_svg)
        else:
            shutil.copy2(svg_file, output_svg)

        log("Generating OpenSCAD file...")
        scad_output = output_path / f"{file_stem}.scad"
        generate_openscad_with_positions(
            svg_file.name,
            valid_positions,
            str(scad_output),
            svg_info=svg_info,
            logger=log,
            target_width=params['target_width'],
            base_thickness=params['base_thickness'],
            outline_offset=params['outline_offset'],
            outline_height=params['outline_height'],
            pyramid_size=params['pyramid_size'],
            pyramid_height=params['pyramid_height'],
            pyramid_style=params['pyramid_style']
        )
        log(f"OpenSCAD file: {scad_output.name}")

        return {
            'success': True,
            'file_stem': file_stem,
            'scad_output': str(scad_output),
            'error': None
        }
    except Exception as e:
        log(f"ERROR: {e}")
        return {
            'success': False,
            'file_stem': file_stem,
            'scad_output': None,
            'error': str(e)
        }


def _stl_render(scad_file_path, stl_file_path, openscad_path, log_queue):
    """STL render worker (runs in ThreadPoolExecutor). subprocess.run releases the GIL,
    so threads here actually overlap with each other and with SVG worker processes.
    """
    file_stem = Path(scad_file_path).stem

    def log(msg):
        log_queue.put(f"[{file_stem}] {msg}")

    log("Rendering STL...")
    try:
        result = subprocess.run(
            [openscad_path, "-o", stl_file_path, scad_file_path],
            capture_output=True, text=True, timeout=300
        )
        if result.returncode == 0:
            log(f"STL file: {Path(stl_file_path).name}")
            return True
        log(f"OpenSCAD error: {result.stderr.strip()}")
        return False
    except subprocess.TimeoutExpired:
        log("Timeout rendering STL")
        return False
    except FileNotFoundError:
        log(f"OpenSCAD executable not found: {openscad_path}")
        return False
    except Exception as e:
        log(f"STL render error: {e}")
        return False


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
        self.target_height = tk.DoubleVar(value=0)  # 0 = auto (maintain aspect ratio)
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
        self.preview_debug = tk.BooleanVar(value=False)

        # Thread pool settings
        self.num_threads = tk.IntVar(value=4)

        # Processing control
        # threading.Event for clean cross-thread signalling — read by GUI/coordinator
        # threads via .is_set(), set by stop button or preview Stop All via .set().
        self.stop_requested = threading.Event()
        self.is_processing = False
        self.preview_continue_event = threading.Event()
        self.preview_approved = False

        # Load saved configuration
        self.load_config()

        self.setup_ui()

        # Save config on window close
        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)

    def setup_ui(self):
        # Create main container
        main_container = ttk.Frame(self.root)
        main_container.pack(fill='both', expand=True)

        # Create notebook for tabs (in the main area)
        notebook = ttk.Notebook(main_container)
        notebook.pack(fill='both', expand=True, padx=10, pady=(10, 5))

        # Tab 1: File Selection
        files_frame = ttk.Frame(notebook)
        notebook.add(files_frame, text="Files & Folders")
        self.setup_files_tab(files_frame)

        # Tab 2: Parameters
        params_frame = ttk.Frame(notebook)
        notebook.add(params_frame, text="Parameters")
        self.setup_parameters_tab(params_frame)

        # Tab 3: Console (renamed from Processing)
        console_frame = ttk.Frame(notebook)
        notebook.add(console_frame, text="Console")
        self.setup_console_tab(console_frame)

        # Create persistent bottom bar with progress and buttons
        self.setup_bottom_bar(main_container)

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
        ttk.Checkbutton(parent, text="Preview debug visualization (pause for approval)",
                       variable=self.preview_debug).grid(row=6, column=1, sticky='w', padx=5, pady=5)

        # Thread pool size
        ttk.Label(parent, text="Parallel Threads:").grid(row=7, column=0, sticky='w', padx=5, pady=5)
        thread_frame = ttk.Frame(parent)
        thread_frame.grid(row=7, column=1, sticky='w', padx=5, pady=5)
        ttk.Spinbox(thread_frame, from_=1, to=16, textvariable=self.num_threads, width=5).pack(side='left')
        ttk.Label(thread_frame, text="(1 = sequential processing)", foreground='gray').pack(side='left', padx=10)

        # Info label
        info_text = "Select the folder containing SVG files and where to output OpenSCAD/STL files.\n" \
                   "Cache folder stores pre-calculated data for faster re-processing."
        ttk.Label(parent, text=info_text, wraplength=700, foreground='gray').grid(
            row=8, column=0, columnspan=3, sticky='w', padx=5, pady=20)

    def setup_parameters_tab(self, parent):
        # SVG Processing Parameters
        group1 = ttk.LabelFrame(parent, text="SVG Processing", padding=10)
        group1.grid(row=0, column=0, sticky='ew', padx=10, pady=5)

        ttk.Label(group1, text="Target Width (X):").grid(row=0, column=0, sticky='w')
        ttk.Entry(group1, textvariable=self.target_width, width=15).grid(row=0, column=1, padx=5)

        ttk.Label(group1, text="Target Height (Y):").grid(row=1, column=0, sticky='w')
        height_frame = ttk.Frame(group1)
        height_frame.grid(row=1, column=1, sticky='w', padx=5)
        ttk.Entry(height_frame, textvariable=self.target_height, width=15).pack(side='left')
        ttk.Label(height_frame, text="(0 = fit to width)", foreground='gray').pack(side='left', padx=5)

        ttk.Label(group1, text="Samples Per Segment:").grid(row=2, column=0, sticky='w')
        ttk.Entry(group1, textvariable=self.samples_per_segment, width=15).grid(row=2, column=1, padx=5)

        ttk.Label(group1, text="Skeleton Resolution:").grid(row=3, column=0, sticky='w')
        ttk.Entry(group1, textvariable=self.skeleton_resolution, width=15).grid(row=3, column=1, padx=5)

        # Scaling note
        scaling_note = "SVG is scaled to fit within target dimensions while maintaining aspect ratio.\n" \
                       "The final size will be the maximum that fits both width and height constraints."
        ttk.Label(group1, text=scaling_note, wraplength=400, foreground='gray').grid(
            row=4, column=0, columnspan=2, sticky='w', pady=(10, 0))

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

    def setup_console_tab(self, parent):
        # Log output
        ttk.Label(parent, text="Processing Log:").pack(anchor='w', padx=10, pady=5)
        self.log_text = scrolledtext.ScrolledText(parent, height=30, width=100)
        self.log_text.pack(fill='both', expand=True, padx=10, pady=5)

        # Clear log button (inside the console tab)
        ttk.Button(parent, text="Clear Log",
                  command=lambda: self.log_text.delete(1.0, tk.END)).pack(anchor='e', padx=10, pady=5)

    def setup_bottom_bar(self, parent):
        """Create persistent bottom bar with progress, status, and control buttons"""
        # Separator
        ttk.Separator(parent, orient='horizontal').pack(fill='x', padx=10)

        # Bottom bar frame
        bottom_bar = ttk.Frame(parent)
        bottom_bar.pack(fill='x', padx=10, pady=10)

        # Left side: Progress bar and status
        progress_frame = ttk.Frame(bottom_bar)
        progress_frame.pack(side='left', fill='x', expand=True)

        # Progress bar
        progress_row = ttk.Frame(progress_frame)
        progress_row.pack(fill='x')
        ttk.Label(progress_row, text="Progress:").pack(side='left', padx=(0, 5))
        self.progress_var = tk.DoubleVar()
        self.progress_bar = ttk.Progressbar(progress_row, variable=self.progress_var, maximum=100, length=300)
        self.progress_bar.pack(side='left', fill='x', expand=True)

        # Status label
        self.status_label = ttk.Label(progress_frame, text="Ready", foreground='green')
        self.status_label.pack(anchor='w', pady=(5, 0))

        # Right side: Buttons
        button_frame = ttk.Frame(bottom_bar)
        button_frame.pack(side='right', padx=(20, 0))

        self.start_button = ttk.Button(button_frame, text="Start Processing",
                                       command=self.start_processing, width=18)
        self.start_button.pack(side='left', padx=5)

        self.stop_button = ttk.Button(button_frame, text="Stop Processing",
                                      command=self.stop_processing,
                                      state='disabled', width=18)
        self.stop_button.pack(side='left', padx=5)

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

    def show_preview_dialog(self, image_path, filename):
        """Show debug visualization preview and wait for user approval (thread-safe)"""
        self.preview_continue_event.clear()
        self.preview_approved = False

        def create_dialog():
            try:
                from PIL import Image, ImageTk
            except ImportError:
                self.log("  WARNING: PIL not available for preview. Install with: pip install Pillow")
                self.preview_approved = True
                self.preview_continue_event.set()
                return

            # Create preview dialog
            dialog = tk.Toplevel(self.root)
            dialog.title(f"Preview: {filename}")
            dialog.transient(self.root)
            dialog.grab_set()

            # Load and display image
            try:
                img = Image.open(image_path)
                # Scale image to fit in dialog (max 800x600)
                max_width, max_height = 900, 700
                ratio = min(max_width / img.width, max_height / img.height)
                if ratio < 1:
                    new_size = (int(img.width * ratio), int(img.height * ratio))
                    img = img.resize(new_size, Image.Resampling.LANCZOS)

                photo = ImageTk.PhotoImage(img)

                # Image label
                img_label = ttk.Label(dialog, image=photo)
                img_label.image = photo  # Keep reference
                img_label.pack(padx=10, pady=10)
            except Exception as e:
                ttk.Label(dialog, text=f"Could not load preview image:\n{str(e)}").pack(padx=20, pady=20)

            # Info label
            ttk.Label(dialog, text=f"Preview for: {filename}",
                     font=('TkDefaultFont', 10, 'bold')).pack(pady=5)
            ttk.Label(dialog, text="Review the debug visualization showing polygon boundary, skeleton, and pyramid positions.",
                     wraplength=500).pack(pady=5)

            # Button frame
            btn_frame = ttk.Frame(dialog)
            btn_frame.pack(pady=15)

            def on_continue():
                self.preview_approved = True
                dialog.destroy()
                self.preview_continue_event.set()

            def on_skip():
                self.preview_approved = False
                dialog.destroy()
                self.preview_continue_event.set()

            def on_stop_all():
                self.preview_approved = False
                self.stop_requested.set()
                dialog.destroy()
                self.preview_continue_event.set()

            ttk.Button(btn_frame, text="Continue", command=on_continue, width=15).pack(side='left', padx=5)
            ttk.Button(btn_frame, text="Skip This File", command=on_skip, width=15).pack(side='left', padx=5)
            ttk.Button(btn_frame, text="Stop All", command=on_stop_all, width=15).pack(side='left', padx=5)

            # Handle window close
            def on_close():
                on_skip()

            dialog.protocol("WM_DELETE_WINDOW", on_close)

            # Center dialog
            dialog.update_idletasks()
            x = self.root.winfo_x() + (self.root.winfo_width() - dialog.winfo_width()) // 2
            y = self.root.winfo_y() + (self.root.winfo_height() - dialog.winfo_height()) // 2
            dialog.geometry(f"+{x}+{y}")

        # Schedule dialog creation in main thread
        self.root.after(0, create_dialog)

        # Wait for user response (blocks the worker thread)
        self.preview_continue_event.wait()

        return self.preview_approved

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
        self.stop_requested.clear()
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
            self.stop_requested.set()
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

        output_path.mkdir(parents=True, exist_ok=True)
        cache_path.mkdir(parents=True, exist_ok=True)

        svg_files = sorted(input_path.glob("*.svg"))

        if not svg_files:
            self.log("WARNING: No SVG files found in input folder")
            self.update_status("No files to process", 'orange')
            return

        num_threads = self.num_threads.get()
        force_sequential = self.preview_debug.get() or num_threads <= 1
        if self.preview_debug.get() and num_threads > 1:
            self.log("NOTE: Preview mode enabled - forcing single-threaded processing")
            num_threads = 1

        self.log(f"Found {len(svg_files)} SVG file(s)")
        if force_sequential:
            self.log("Running sequentially (in-process)")
        else:
            self.log(f"Using {num_threads} worker process(es) for SVG + {num_threads} thread(s) for STL render")
        self.update_status(f"Processing {len(svg_files)} files...", 'blue')

        if force_sequential:
            self._run_sequential(svg_files, output_path, cache_path)
        else:
            self._run_parallel(svg_files, output_path, cache_path, num_threads)

    def _run_sequential(self, svg_files, output_path, cache_path):
        """Sequential in-process path. Used when preview_debug is on or num_threads<=1."""
        files_processed = 0
        for idx, svg_file in enumerate(svg_files):
            if self.stop_requested.is_set():
                break
            try:
                self.log(f"\n{'='*60}")
                self.log(f"Processing: {svg_file.name}")
                self.log(f"{'='*60}")
                self.process_single_svg(svg_file, output_path, cache_path)
                files_processed += 1
            except Exception as e:
                self.log(f"ERROR processing {svg_file.name}: {e}")
            progress = ((idx + 1) / len(svg_files)) * 100
            self.root.after(0, lambda p=progress: self.progress_var.set(p))

        self._finalize_run(files_processed, len(svg_files))

    def _run_parallel(self, svg_files, output_path, cache_path, num_threads):
        """Parallel path: process pool for SVG, thread pool for STL render.

        SVG work is CPU-bound Python (skeleton calculation), so it needs separate processes
        to bypass the GIL. STL render is subprocess.run, so threads suffice and they overlap
        with SVG worker processes for higher throughput.
        """
        params = self._collect_params()
        use_cache = self.use_cache.get()
        generate_stl = self.generate_stl.get()
        openscad_path = self.openscad_path.get()

        total_units = len(svg_files) * (2 if generate_stl else 1)
        completed_units = [0]
        units_lock = threading.Lock()
        files_processed = [0]

        def bump_progress():
            with units_lock:
                completed_units[0] += 1
                pct = (completed_units[0] / total_units) * 100
            self.root.after(0, lambda p=pct: self.progress_var.set(p))

        manager = multiprocessing.Manager()
        log_queue = manager.Queue()
        log_pump_stop = threading.Event()

        def log_pump():
            while not log_pump_stop.is_set():
                try:
                    msg = log_queue.get(timeout=0.1)
                except queue.Empty:
                    continue
                self.log(msg)

        pump_thread = threading.Thread(target=log_pump, daemon=True)
        pump_thread.start()

        svg_pool = ProcessPoolExecutor(max_workers=num_threads)
        stl_pool = ThreadPoolExecutor(max_workers=num_threads, thread_name_prefix="STLWorker")
        stl_futures = []

        try:
            svg_futures = {
                svg_pool.submit(_svg_worker, str(f), str(output_path), str(cache_path),
                                params, use_cache, log_queue): f
                for f in svg_files
            }

            for future in as_completed(svg_futures):
                if self.stop_requested.is_set():
                    for f in svg_futures:
                        f.cancel()
                    break

                result = future.result()
                bump_progress()

                if result['success']:
                    files_processed[0] += 1
                    if generate_stl and result['scad_output']:
                        scad_path = result['scad_output']
                        stl_path = str(Path(scad_path).with_suffix('.stl'))
                        stl_future = stl_pool.submit(
                            _stl_render, scad_path, stl_path, openscad_path, log_queue
                        )
                        stl_future.add_done_callback(lambda f: bump_progress())
                        stl_futures.append(stl_future)
                else:
                    self.log(f"ERROR processing {result['file_stem']}: {result['error']}")

            # Drain pending STL renders before returning
            for sf in as_completed(stl_futures):
                if self.stop_requested.is_set():
                    break
                sf.result()
        finally:
            svg_pool.shutdown(wait=not self.stop_requested.is_set(), cancel_futures=True)
            stl_pool.shutdown(wait=not self.stop_requested.is_set(), cancel_futures=True)
            log_pump_stop.set()
            pump_thread.join(timeout=1.0)
            manager.shutdown()

        self._finalize_run(files_processed[0], len(svg_files))

    def _collect_params(self):
        """Snapshot all tk vars to a plain dict (picklable, safe for worker processes)."""
        return {
            'target_width': self.target_width.get(),
            'target_height': self.target_height.get(),
            'samples_per_segment': self.samples_per_segment.get(),
            'skeleton_resolution': self.skeleton_resolution.get(),
            'pyramid_size': self.pyramid_size.get(),
            'pyramid_spacing': self.pyramid_spacing.get(),
            'safety_margin': self.safety_margin.get(),
            'include_rotation': self.include_rotation.get(),
            'base_thickness': self.base_thickness.get(),
            'outline_offset': self.outline_offset.get(),
            'outline_height': self.outline_height.get(),
            'pyramid_height': self.pyramid_height.get(),
            'pyramid_style': self.pyramid_style.get(),
        }

    def _finalize_run(self, files_processed, total):
        if self.stop_requested.is_set():
            self.log(f"\n{'='*60}")
            self.log("Processing stopped by user")
            self.log(f"Processed {files_processed}/{total} files")
            self.log(f"{'='*60}")
            self.update_status(f"Stopped ({files_processed}/{total} completed)", 'orange')
            return
        self.root.after(0, lambda: self.progress_var.set(100))
        self.log(f"\n{'='*60}")
        self.log("Processing complete!")
        self.log(f"Processed {files_processed}/{total} files successfully")
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
                target_height=self.target_height.get() if self.target_height.get() > 0 else None,
                samples_per_segment=self.samples_per_segment.get(),
                logger=self.log
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
                safety_margin=self.safety_margin.get(),
                skeleton_points=skeleton_points,
                logger=self.log
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

        # Show debug visualization preview if enabled
        if self.preview_debug.get():
            self.log(f"  Generating debug visualization...")
            debug_viz_path = output_path / f"{file_stem}_debug.png"
            save_debug_visualization(polygon, skeleton_points, valid_positions, str(debug_viz_path), logger=self.log)
            self.log(f"  Waiting for preview approval...")

            approved = self.show_preview_dialog(str(debug_viz_path), svg_file.name)

            if self.stop_requested.is_set():
                self.log(f"  Processing stopped by user")
                raise Exception("Processing stopped by user")

            if not approved:
                self.log(f"  ✗ Skipped by user")
                return  # Skip this file

            self.log(f"  ✓ Approved, continuing...")

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
            logger=self.log,
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
        _save_cache(cache_file, polygon, svg_info, skeleton_points, valid_positions)

    def load_from_cache(self, cache_file):
        return _load_cache(cache_file)

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
            'target_height': self.target_height.get(),
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
            'use_cache': self.use_cache.get(),
            'preview_debug': self.preview_debug.get(),
            'num_threads': self.num_threads.get()
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
            if 'target_height' in config:
                self.target_height.set(config['target_height'])
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
            if 'preview_debug' in config:
                self.preview_debug.set(config['preview_debug'])
            if 'num_threads' in config:
                self.num_threads.set(config['num_threads'])

        except Exception as e:
            print(f"Warning: Could not load config: {e}")

    def on_closing(self):
        """Save config and close application"""
        self.save_config()
        self.root.destroy()


def main():
    multiprocessing.freeze_support()
    root = tk.Tk()
    app = BulkProcessorGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
