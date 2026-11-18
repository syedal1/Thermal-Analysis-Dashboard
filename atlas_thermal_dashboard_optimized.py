#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Atlas Thermal Sensor Dashboard - Interactive Web Application
Version 3.2 - Original interface with disk-backed analysis and lot comparison

Features:
- Dashboard: Overview of all products and tests
- Product View: Select and analyze individual products
- Test Analysis: Deep dive into specific tests with parameter comparison
- Data Filter: Dynamic filtering controls
- Plot Settings: Customizable visualization options
- Configuration: Manage test configurations
- Data Upload: Upload and process new data files
"""

import dash
from dash import dcc, html, dash_table, Input, Output, State, ALL, callback_context
from large_data_store import ThermalStore
import profiles as profile_store
import dash_bootstrap_components as dbc
import plotly.graph_objects as go
import plotly.express as px
import plotly.io as pio
import pandas as pd
import numpy as np
import json
import os
import glob
import pickle
import logging
import io
import base64
import hashlib
import re
import sys
import threading
from datetime import datetime
from pathlib import Path
from PIL import Image
from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Image as RLImage, Paragraph, Spacer, PageBreak
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch

# ============================================================================
# UTF-8 ENCODING SETUP - Fix emoji and special character encoding issues
# ============================================================================

# Force UTF-8 encoding for stdout/stderr (fixes emoji in print statements on Windows)
if sys.stdout.encoding != 'utf-8':
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass
if sys.stderr.encoding != 'utf-8':
    try:
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

def safe_print(*args, **kwargs):
    """Print function that handles encoding errors gracefully.
    Replaces unencodable characters instead of crashing.
    """
    try:
        print(*args, **kwargs)
    except UnicodeEncodeError:
        # Fallback: encode with error replacement
        text = ' '.join(str(arg) for arg in args)
        try:
            encoded = text.encode(sys.stdout.encoding or 'utf-8', errors='replace')
            decoded = encoded.decode(sys.stdout.encoding or 'utf-8', errors='replace')
            print(decoded, **kwargs)
        except Exception:
            # Last resort: strip non-ASCII characters
            text_ascii = text.encode('ascii', errors='replace').decode('ascii')
            print(text_ascii, **kwargs)

# Setup logging
logger = logging.getLogger(__name__)

# ============================================================================
# UNICODE SANITIZATION - Fix surrogates and encoding issues
# ============================================================================

def _sanitize_unicode(s):
    """Remove invalid Unicode characters (surrogates) that cause encoding errors.
    Converts string to safe UTF-8 form that won't cause 'surrogates not allowed' errors.
    """
    if s is None:
        return None
    if not isinstance(s, str):
        s = str(s)
    # Replace any surrogate characters and other problematic sequences
    try:
        # Encode to UTF-8 and back, which removes surrogates
        return s.encode('utf-8', errors='replace').decode('utf-8', errors='replace')
    except Exception:
        # Fallback: remove non-printable characters
        return re.sub(r'[\x00-\x1f\x7f-\x9f]', '', s)

def _sanitize_dataframe(df):
    """Sanitize all string columns in a DataFrame to remove invalid Unicode."""
    if df is None or df.empty:
        return df
    for col in df.columns:
        if df[col].dtype == 'object':
            df[col] = df[col].apply(lambda x: _sanitize_unicode(x) if isinstance(x, str) else x)
    return df

# Configuration
OUTPUT_FOLDER = "output"
CONFIG_FOLDER = "config"
DATA_FOLDER = "input"
SIGMA_LEVEL = 10
LIMITS_FILE = "Product_Test_Limits.json"  # per-product per-test default UCL/LCL

# Data Persistence Configuration
PERSISTENT_DATA_FOLDER = "Saved_Sessions"  # Folder to store saved dashboard data
PERSISTENT_DATA_FILE = "dashboard_data_cache.pkl"  # Main data cache file
PERSISTENT_METADATA_FILE = "data_metadata.json"  # Metadata about cached data

# Test Program directories (UNC network shares)
PROGRAM_DIRS = [
    r"\\al4file1.ra.intel.com\sdx\program\1278*",
    r"\\al4file1.ra.intel.com\sdx\program\1001*",
]
PROGRAM_MODULES = ["PTH_DTS", "PTH_DIODE", "PTH_THRSOAK"]

# Product mapping - prefix patterns (DevRevStep startswith) -> product name
PRODUCT_MAPPING = {
    '8PY6CV': 'NVL_Cdie_48_N2P',
    '8PF6CV': 'NVL_Cdie_816_N2P',
    '8PF5CV': 'NVL_Cdie_816_BLLC_N2P',
    '8PF4CV': 'NVL_Cdie_816_18A',
}

PRODUCT_DISPLAY_NAMES = {
    'NVL_Cdie_48_N2P': 'NVL Cdie 48 (N2P)',
    'NVL_Cdie_816_N2P': 'NVL Cdie 816 (N2P)',
    'NVL_Cdie_816_BLLC_N2P': 'NVL Cdie 816 BLLC (N2P)',
    'NVL_Cdie_816_18A': 'NVL Cdie 816 (18A)',
}

COLORS = {
    'primary': '#0066cc',
    'secondary': '#4a90e2',
    'success': '#28a745',
    'danger': '#dc3545',
    'warning': '#ffc107',
    'info': '#17a2b8',
    'light_bg': '#f8f9fa',
    'dark_text': '#212529',
    'sidebar': '#2c3e50',
    'sidebar_active': '#3498db'
}

# Test grouping patterns - wildcard matching for similar tests
TOKEN_PATTERNS = [
    '*_BGTRIMS2_BGTRIMCODE_PASS',
    '*_CATTRIM_PASS',
    '*_DTSCODE_SDS_AVG_PASS',
    '*_SDS_VBE',
    '*_SDS_VBG',
    '*_SDS_VREFOUT',
    '*_DTSTEMP_AVG_PASS',
    '*_SDS_DTSOFFSET',
    '*_SDT_DTSOFFSET'
]

# Initialize Dash app
app = dash.Dash(__name__, external_stylesheets=[dbc.themes.BOOTSTRAP], suppress_callback_exceptions=True)
app.title = "Thermal Analysis Dashboard"

# Global data storage
class DataStore:
    def __init__(self):
        self.backend = None
        self.database = Path(PERSISTENT_DATA_FOLDER) / 'thermal_large.duckdb'
        self.data_folder = DATA_FOLDER
        self.active_profile = None  # set when a work-week profile is open instead of the main dataset
        self.memory_limit = '1GB'
        self.batch_size = 5000
        self.threads = 1
        self.overview = {}
        self.df = None
        self.config_data = None
        self.product_limits = None
        self.loaded = False
        self.load_status = "Not loaded"
        self.load_error = None
        
        # 3-tier caching for instant tab switching
        self._product_list_cache = None
        self._test_groups_cache = None
        self._unique_values_cache = {}
    
    def _invalidate_caches(self):
        """Reset all caches after data load"""
        self._product_list_cache = None
        self._test_groups_cache = None
        self._unique_values_cache = {}
    
    def get_products_cached(self):
        """Get list of unique products from cache, computing if needed"""
        if self._product_list_cache is None:
            if self.loaded:
                self._product_list_cache = self.backend.values('Product_Name')
            else:
                self._product_list_cache = []
        return self._product_list_cache
    
    def get_test_groups_cached(self):
        """Get test groupings from cache, computing if needed"""
        if self._test_groups_cache is None:
            if self.loaded:
                tests = self.backend.values('Test')
                self._test_groups_cache = group_tests_by_pattern(tests)
            else:
                self._test_groups_cache = {}
        return self._test_groups_cache
    
    def get_unique_values_cached(self, column):
        """Get unique values for a column from cache, computing if needed"""
        if column not in self._unique_values_cache:
            if self.loaded:
                values = [item['value'] for item in self.backend.options(column)]
                self._unique_values_cache[column] = values
            else:
                self._unique_values_cache[column] = []
        return self._unique_values_cache[column]
    
    def load_data(self):
        """Load data and configuration from Input_data folder and Config folder"""
        if not self.loaded:
            try:
                self.load_status = "Loading..."
                self.config_data = load_config_from_folder()
                self.product_limits = load_product_limits()
                if self.backend is None:
                    self.backend = ThermalStore(self.database, self.memory_limit, self.batch_size, self.threads)
                self.backend.refresh(self.data_folder, self.config_data, PRODUCT_MAPPING)
                self.overview = self.backend.overview()
                self.df = self.backend.plot_rows(limit=1)
                
                if self.df is not None and not self.df.empty:
                    self._invalidate_caches()  # Reset caches after data load
                    self.loaded = True
                    source = f"profile '{self.active_profile['work_week']}'" if self.active_profile else "disk-backed"
                    self.load_status = f"✅ Loaded {self.overview['readings']:,} readings ({source})"
                    self.load_error = None
                else:
                    self.loaded = False
                    self.load_status = f"⚠️ No data files found in {self.data_folder}"
                    self.load_error = "No data files detected"
                    self.df = pd.DataFrame()
            except Exception as e:
                self.loaded = False
                self.load_status = f"❌ Error loading data"
                self.load_error = str(e)
                self.df = pd.DataFrame()
                print(f"Data load error: {e}")
        return self.loaded

    def switch_to_profile(self, slug):
        """Point this data store at a saved work-week profile's isolated cache and reload."""
        profile = profile_store.load_profile(slug)
        if self.backend is not None:
            self.backend.close()
        self.backend = None
        self.loaded = False
        self.database = profile_store.profile_database_path(profile['slug'])
        self.data_folder = profile_store.profile_input_folder(profile['slug'])
        self.active_profile = profile
        self._invalidate_caches()
        self.load_data()
        return profile

    def switch_to_main(self):
        """Return this data store to the shared main dataset in input/."""
        if self.backend is not None:
            self.backend.close()
        self.backend = None
        self.loaded = False
        self.database = Path(PERSISTENT_DATA_FOLDER) / 'thermal_large.duckdb'
        self.data_folder = DATA_FOLDER
        self.active_profile = None
        self._invalidate_caches()
        self.load_data()

data_store = DataStore()
# Data will be loaded on first access via load_data() call in content functions

# ============================================================================
# DATA PERSISTENCE FUNCTIONS - Save/Load Dashboard Data Cache
# ============================================================================

def _get_input_data_hash():
    """Calculate a hash based on all input data files (size and mtime).
    Used to detect when input data has changed.
    """
    data_files = []
    for ext in ['*.xlsx', '*.xls', '*.csv', '*.txt']:
        data_files.extend(glob.glob(os.path.join(DATA_FOLDER, ext)))
    
    data_files = [f for f in data_files if not os.path.basename(f).startswith('~$')]
    
    hash_data = []
    for file_path in sorted(data_files):
        try:
            st = os.stat(file_path)
            hash_data.append(f"{file_path}:{st.st_mtime}:{st.st_size}")
        except OSError:
            pass
    
    hash_str = "|".join(hash_data)
    return hashlib.md5(hash_str.encode()).hexdigest()


def save_dashboard_data(df):
    """Save processed dashboard dataframe to persistent cache.
    
    Args:
        df: Processed pandas DataFrame containing all dashboard data
    
    Returns:
        bool: True if save was successful, False otherwise
    """
    try:
        # Sanitize data before saving to prevent Unicode issues on load
        df = _sanitize_dataframe(df)
        
        os.makedirs(PERSISTENT_DATA_FOLDER, exist_ok=True)
        
        cache_path = os.path.join(PERSISTENT_DATA_FOLDER, PERSISTENT_DATA_FILE)
        metadata_path = os.path.join(PERSISTENT_DATA_FOLDER, PERSISTENT_METADATA_FILE)
        
        # Save the dataframe
        with open(cache_path, 'wb') as f:
            pickle.dump(df, f, protocol=pickle.HIGHEST_PROTOCOL)
        
        # Save metadata about when this was saved and what data it was from
        metadata = {
            'saved_timestamp': datetime.now().isoformat(),
            'data_hash': _get_input_data_hash(),
            'row_count': len(df),
            'columns': list(df.columns),
            'version': '1.0'
        }
        
        with open(metadata_path, 'w') as f:
            json.dump(metadata, f, indent=2)
        
        safe_print(f"✅ Dashboard data saved: {cache_path}")
        safe_print(f"   Rows: {len(df):,}, Columns: {len(df.columns)}")
        return True
        
    except Exception as e:
        safe_print(f"⚠️ Error saving dashboard data: {e}")
        return False


def load_dashboard_data():
    """Load previously saved dashboard dataframe from persistent cache.
    
    Returns:
        tuple: (DataFrame, metadata_dict) if cache is valid, (None, None) if not
    """
    try:
        cache_path = os.path.join(PERSISTENT_DATA_FOLDER, PERSISTENT_DATA_FILE)
        metadata_path = os.path.join(PERSISTENT_DATA_FOLDER, PERSISTENT_METADATA_FILE)
        
        # Check if cache files exist
        if not os.path.exists(cache_path) or not os.path.exists(metadata_path):
            print("  No cached data found")
            return None, None
        
        # Check cache file size (warn if too large)
        try:
            cache_size_mb = os.path.getsize(cache_path) / (1024 * 1024)
            if cache_size_mb > 500:
                safe_print(f"  ⚠️ Cache file is large ({cache_size_mb:.0f}MB), loading may take a while...")
        except Exception:
            pass
        
        # Load and verify metadata
        safe_print("  Loading cache metadata...")
        with open(metadata_path, 'r', encoding='utf-8') as f:
            metadata = json.load(f)
        
        # Check if input data has changed
        safe_print("  Checking input data hash...")
        current_hash = _get_input_data_hash()
        saved_hash = metadata.get('data_hash', '')
        
        if current_hash != saved_hash:
            safe_print("  ⚠️ Input data has changed. Will reload from source files.")
            return None, None
        
        # Load the cached dataframe (this might take a while for large files)
        safe_print("  Loading cached dataframe (this may take a minute for large files)...")
        try:
            with open(cache_path, 'rb') as f:
                df = pickle.load(f)
        except Exception as e:
            safe_print(f"  ⚠️ Error loading pickle file: {e}")
            safe_print("  Will reload from source files instead")
            return None, None
        
        # Sanitize loaded data to prevent Unicode encoding errors
        safe_print("  Sanitizing loaded data...")
        df = _sanitize_dataframe(df)
        
        saved_time = metadata.get('saved_timestamp', 'unknown')
        safe_print(f"  ✅ Loaded cached dashboard data (Saved: {saved_time})")
        safe_print(f"     Rows: {len(df):,}, Columns: {len(df.columns)}")
        
        return df, metadata
        
    except Exception as e:
        safe_print(f"  ⚠️ Unexpected error loading cached dashboard data: {e}")
        return None, None


def is_data_cache_valid():
    """Check if cached data exists and is valid (hasn't expired or been invalidated).
    
    Returns:
        bool: True if cache exists and is valid, False otherwise
    """
    if data_store.backend is not None:
        return data_store.backend.is_current(DATA_FOLDER, load_config_from_folder(), PRODUCT_MAPPING)
    metadata_path = os.path.join(PERSISTENT_DATA_FOLDER, PERSISTENT_METADATA_FILE)
    cache_path = os.path.join(PERSISTENT_DATA_FOLDER, PERSISTENT_DATA_FILE)
    
    if not os.path.exists(cache_path) or not os.path.exists(metadata_path):
        return False
    
    try:
        with open(metadata_path, 'r') as f:
            metadata = json.load(f)
        
        current_hash = _get_input_data_hash()
        saved_hash = metadata.get('data_hash', '')
        
        return current_hash == saved_hash
    except Exception:
        return False


def clear_dashboard_cache():
    """Clear the persistent dashboard data cache.
    Useful when you want to force a reload from source files on next launch.
    
    Returns:
        bool: True if clear was successful, False otherwise
    """
    try:
        if data_store.backend is not None:
            data_store.backend.invalidate()
            data_store._invalidate_caches()
            return True
        cache_path = os.path.join(PERSISTENT_DATA_FOLDER, PERSISTENT_DATA_FILE)
        metadata_path = os.path.join(PERSISTENT_DATA_FOLDER, PERSISTENT_METADATA_FILE)
        
        removed_files = []
        
        if os.path.exists(cache_path):
            os.remove(cache_path)
            removed_files.append(cache_path)
        
        if os.path.exists(metadata_path):
            os.remove(metadata_path)
            removed_files.append(metadata_path)
        
        if removed_files:
            print(f"✅ Cleared dashboard cache ({len(removed_files)} files removed)")
            for f in removed_files:
                print(f"   - {f}")
        else:
            print("ℹ️ No cache files to clear")
        
        return True
    except Exception as e:
        safe_print(f"⚠️ Error clearing cache: {e}")
        return False


# ============================================================================
# DATA LOADING FUNCTIONS
# ============================================================================

def load_config_from_folder():
    """Load all configuration files from Config folder"""
    config_data = {}
    config_files = glob.glob(os.path.join(CONFIG_FOLDER, '*.json'))
    
    for config_file in config_files:
        # Skip the product-limits config (different schema, loaded separately)
        if os.path.basename(config_file) == LIMITS_FILE:
            continue
        try:
            with open(config_file, 'r') as f:
                config = json.load(f)
                if isinstance(config, list):
                    for test_config in config:
                        test_name = test_config.get('TEST_NAME')
                        if test_name:
                            # Parse COLUMN_NAMES if it's a comma-separated string
                            column_names = test_config.get('COLUMN_NAMES', [])
                            if isinstance(column_names, str):
                                # Split by comma and strip whitespace
                                column_names = [col.strip() for col in column_names.split(',')]
                                test_config['COLUMN_NAMES'] = column_names
                            config_data[test_name] = test_config
        except Exception as e:
            safe_print(f"Error loading {config_file}: {e}")
    
    return config_data


def load_product_limits():
    """Load per-product per-test default UCL/LCL spec limits.
    Format: { product_name: [ { 'test': str, 'UCL': num, 'LCL': num }, ... ], ... }
    """
    path = os.path.join(CONFIG_FOLDER, LIMITS_FILE)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, 'r') as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
        # Strip comment keys (those starting with '_')
        cleaned = {k: v for k, v in data.items() if not str(k).startswith('_') and isinstance(v, list)}
        print(f"Loaded product spec limits for {len(cleaned)} product(s)")
        return cleaned
    except Exception as e:
        safe_print(f"Error loading {path}: {e}")
        return {}


def find_spec_limits(product_name, test_name, limits):
    """Return (UCL, LCL) spec limits for given product+test, or (None, None).
    Matching is done as case-insensitive substring of test_name; longer keys win.
    """
    if not limits or not product_name or not test_name:
        return None, None
    entries = limits.get(product_name)
    if not entries:
        return None, None
    test_upper = str(test_name).upper()
    for entry in sorted(entries, key=lambda e: len(str(e.get('test', ''))), reverse=True):
        key = str(entry.get('test') or '').upper()
        if key and key in test_upper:
            ucl = entry.get('UCL')
            lcl = entry.get('LCL')
            try:
                ucl = float(ucl) if ucl is not None else None
                lcl = float(lcl) if lcl is not None else None
            except (TypeError, ValueError):
                ucl, lcl = None, None
            return ucl, lcl
    return None, None

def _read_data_file(file_path):
    """Read a single data file (Excel/CSV/TXT), only the columns we need.
    Uses proper encoding to prevent Unicode errors.
    """
    needed = [
        'TEST_NAME', 'TEST_RESULT', 'VISUAL_ID', 'Lot', 'LOTS', 'DevRevStep',
        'FUNCTIONAL_BIN', 'Thermal Head Id', 'Unit Tester Id',
        'Unit Tester Site Id', 'Unit TIU', 'TIU Personality Card ID',
        'Program Name', 'Program_Name',
    ]
    def _read_excel(fp, **kw):
        # Try the much-faster calamine engine first (python-calamine); fall back to default.
        try:
            return pd.read_excel(fp, engine='calamine', **kw)
        except Exception:
            return pd.read_excel(fp, **kw)

    if file_path.endswith(('.xlsx', '.xls')):
        try:
            header = _read_excel(file_path, nrows=0).columns.tolist()
            usecols = [c for c in header if c in needed]
            df = _read_excel(file_path, usecols=usecols or None)
            return _sanitize_dataframe(df)
        except Exception as e:
            print(f"  Error reading Excel file {file_path}: {e}")
            try:
                return _sanitize_dataframe(_read_excel(file_path))
            except Exception as e2:
                print(f"  Failed to read {file_path}: {e2}")
                return None
    if file_path.endswith('.csv'):
        try:
            # Try UTF-8 with BOM handling first
            header = pd.read_csv(file_path, nrows=0, encoding='utf-8-sig', on_bad_lines='skip').columns.tolist()
            usecols = [c for c in header if c in needed]
            df = pd.read_csv(file_path, usecols=usecols or None, encoding='utf-8-sig', on_bad_lines='skip')
            return _sanitize_dataframe(df)
        except Exception:
            try:
                # Fallback to latin-1 which accepts any byte sequence
                df = pd.read_csv(file_path, encoding='latin-1', on_bad_lines='skip')
                return _sanitize_dataframe(df)
            except Exception as e:
                print(f"  Error reading CSV file {file_path}: {e}")
                return None
    if file_path.endswith('.txt'):
        try:
            # Try UTF-8 with BOM handling first
            header = pd.read_csv(file_path, sep='\t', nrows=0, encoding='utf-8-sig', on_bad_lines='skip').columns.tolist()
            usecols = [c for c in header if c in needed]
            df = pd.read_csv(file_path, sep='\t', usecols=usecols or None, encoding='utf-8-sig', on_bad_lines='skip')
            return _sanitize_dataframe(df)
        except Exception:
            try:
                # Fallback to latin-1
                df = pd.read_csv(file_path, sep='\t', encoding='latin-1', on_bad_lines='skip')
                return _sanitize_dataframe(df)
            except Exception as e:
                print(f"  Error reading TXT file {file_path}: {e}")
                return None
    return None


def _transform_file_to_long(df_temp, config_data):
    """Memory-efficient wide -> long transform: split TEST_RESULT by '|', explode, map sensor names.
    Processes data row-by-row to avoid memory allocation errors with large datasets.
    Sanitizes all string data to prevent Unicode encoding errors.
    """
    if df_temp is None or df_temp.empty:
        return None
    if 'TEST_RESULT' not in df_temp.columns or 'TEST_NAME' not in df_temp.columns:
        return None
    
    # Drop rows without test_result before doing string work
    df = df_temp.copy()
    df = df[df['TEST_RESULT'].notna()]
    df['TEST_RESULT'] = df['TEST_RESULT'].astype(str)
    df = df[df['TEST_RESULT'] != '']
    if df.empty:
        return None
    
    # Sensor name lookup: TEST_NAME -> list of sensor names
    sensor_map = {t: cfg.get('COLUMN_NAMES', []) for t, cfg in (config_data or {}).items()}
    
    # Process row-by-row to avoid memory allocation errors with large arrays
    # This avoids the massive NumPy array allocation that occurs with explode() on huge datasets
    output_rows = []
    
    for idx, row in df.iterrows():
        try:
            test_name = row['TEST_NAME']
            test_result_str = str(row['TEST_RESULT']).strip()
            
            if not test_result_str or test_result_str == 'nan':
                continue
            
            # Split the test result by pipe
            values = test_result_str.split('|')
            if not values:
                continue
            
            # Get sensor names for this test
            sensors = sensor_map.get(test_name, [])
            n_values = len(values)
            
            # Build sensor names list
            if isinstance(sensors, list) and sensors:
                sensor_names = [sensors[i] if i < len(sensors) else f"Sensor_{i}" for i in range(n_values)]
            else:
                sensor_names = [f"Sensor_{i}" for i in range(n_values)]
            
            # Convert values to numeric
            numeric_values = []
            for val in values:
                try:
                    numeric_values.append(float(val.strip()))
                except (ValueError, AttributeError):
                    numeric_values.append(None)
            
            # Build output rows, skipping non-numeric values
            for sensor_name, numeric_val in zip(sensor_names, numeric_values):
                if numeric_val is not None:  # Skip non-numeric values
                    output_rows.append({
                        'Test': test_name,
                        'Measurement_Point': sensor_name,
                        'Value': numeric_val,
                        'Unit_ID': row.get('VISUAL_ID', ''),
                        'Lot': row.get('Lot', row.get('LOTS', '')),
                        'DevRevStep': row.get('DevRevStep', ''),
                        'Functional_Bin': row.get('FUNCTIONAL_BIN', ''),
                        'Thermal_Head_Id': row.get('Thermal Head Id', ''),
                        'Unit_Tester_Id': row.get('Unit Tester Id', ''),
                        'Unit_Tester_Site_Id': row.get('Unit Tester Site Id', ''),
                        'Unit_TIU': row.get('Unit TIU', ''),
                        'TIU_Personality_Card_ID': row.get('TIU Personality Card ID', ''),
                        'Program_Name': row.get('Program Name', row.get('Program_Name', '')),
                    })
        except Exception as e:
            print(f"  Warning: Error processing row {idx}: {e}")
            continue
    
    if not output_rows:
        return None
    
    # Create DataFrame from list of dictionaries (more memory-efficient than concat)
    out = pd.DataFrame(output_rows)
    # Sanitize all string data to prevent Unicode encoding errors in responses
    return _sanitize_dataframe(out)


def load_data_from_folder():
    """Load all data files from Input_data folder and transform to long format.
    First checks for persistent cache from previous session.
    Then loads from source files if needed.
    Uses per-file parquet cache keyed on mtime+size for fast re-loads.
    All preprocessing is handled internally - no external script dependencies.
    Sanitizes all data to prevent Unicode encoding errors.
    """
    
    safe_print("\n" + "="*70)
    safe_print("📊 Data Loading Pipeline (All-in-One)")
    safe_print("="*70)
    
    # Step 1: Try to load from persistent cache first
    safe_print("📁 Checking persistent cache...")
    
    cached_df, metadata = load_dashboard_data()
    if cached_df is not None:
        safe_print("✨ Using cached dashboard data (fast load)")
        return cached_df
    
    safe_print("📁 Loading from source data files...")
    
    # Step 2: Load from source files if cache not available
    all_data = []
    data_files = []
    config_data = load_config_from_folder()
    
    cache_dir = os.path.join(DATA_FOLDER, '.cache')
    os.makedirs(cache_dir, exist_ok=True)
    
    for ext in ['*.xlsx', '*.xls', '*.csv', '*.txt']:
        data_files.extend(glob.glob(os.path.join(DATA_FOLDER, ext)))
    
    # Skip Excel lock files (~$...)
    data_files = [f for f in data_files if not os.path.basename(f).startswith('~$')]
    
    for file_path in data_files:
        try:
            base = os.path.basename(file_path)
            try:
                st = os.stat(file_path)
                cache_key = f"{base}__{int(st.st_mtime)}__{st.st_size}.parquet"
            except OSError:
                cache_key = None
            cache_path = os.path.join(cache_dir, cache_key) if cache_key else None
            
            if cache_path and os.path.exists(cache_path):
                t0 = datetime.now()
                df_expanded = pd.read_parquet(cache_path)
                dt = (datetime.now() - t0).total_seconds()
                print(f"  Loaded {base} from cache: {len(df_expanded):,} rows ({dt:.1f}s)")
                all_data.append(df_expanded)
                continue
            
            print(f"  Loading {base}...")
            t0 = datetime.now()
            df_temp = _read_data_file(file_path)
            if df_temp is None:
                continue
            
            df_expanded = _transform_file_to_long(df_temp, config_data)
            if df_expanded is None or df_expanded.empty:
                print(f"    Skipping {base} - no TEST_RESULT column or no rows")
                continue
            
            dt = (datetime.now() - t0).total_seconds()
            print(f"    Expanded {len(df_expanded):,} sensor readings from {len(df_temp):,} test rows ({dt:.1f}s)")
            
            # Save to cache (parquet for speed)
            if cache_path:
                try:
                    df_expanded.to_parquet(cache_path, index=False)
                except Exception as e:
                    print(f"    (cache write skipped: {e})")
            
            all_data.append(df_expanded)
        except Exception as e:
            print(f"  Error loading {file_path}: {e}")
    
    if all_data:
        df = pd.concat(all_data, ignore_index=True)
        safe_print(f"✅ Total data loaded: {len(df):,} sensor readings")
        
        # Ensure all required columns exist
        required_columns = ['Test', 'Value', 'Unit_ID', 'Measurement_Point']
        missing_columns = [col for col in required_columns if col not in df.columns]
        
        if missing_columns:
            safe_print(f"⚠️ Warning: Missing columns {missing_columns}")
            for col in missing_columns:
                df[col] = ''
        
        # Sanitize all string data before saving and returning
        df = _sanitize_dataframe(df)
        
        # Step 3: Save to persistent cache for next launch
        save_dashboard_data(df)
        
        return df
    
    safe_print("⚠️ No data loaded")
    return pd.DataFrame()

def _map_devrev_to_product(devrev):
    """Map a DevRevStep value to a product name using prefix matching."""
    if devrev is None:
        return 'Unknown_Product_'
    s = _sanitize_unicode(str(devrev))
    # Try longest prefix first so '8PF6CVER' would still pick '8PF6CV' over a shorter match
    for prefix in sorted(PRODUCT_MAPPING.keys(), key=len, reverse=True):
        if s.startswith(prefix):
            return _sanitize_unicode(PRODUCT_MAPPING[prefix])
    return _sanitize_unicode(f'Unknown_Product_{s}')


def extract_parameters_from_dataframe(df):
    """Extract product information from DevRevStep column"""
    dev_col = None
    for col in df.columns:
        if 'devrev' in col.lower():
            dev_col = col
            break
    
    if dev_col:
        df['DevRevStep'] = df[dev_col].apply(_sanitize_unicode)
        df['Product_Name'] = df['DevRevStep'].map(_map_devrev_to_product)
        df['Product_Short'] = df['Product_Name'].apply(
            lambda x: _sanitize_unicode(x.replace('_', ' ').replace('NVL Cdie', 'C').replace('Unknown Product', 'Unk'))
        )
        df['Product_Display'] = df['Product_Name'].map(
            lambda x: _sanitize_unicode(PRODUCT_DISPLAY_NAMES.get(x, x.replace('_', ' ')))
        )
    
    return df
    
    return df

def calculate_control_limits(values, sigma=10):
    """Calculate control limits"""
    mean = np.mean(values)
    std = np.std(values)
    ucl = mean + sigma * std
    lcl = mean - sigma * std
    return mean, ucl, lcl

def match_test_to_pattern(test_name, pattern):
    """Match test name to pattern with wildcard support"""
    import re
    # Convert wildcard pattern to regex
    regex_pattern = pattern.replace('*', '.*')
    return re.match(f'^{regex_pattern}$', test_name, re.IGNORECASE) is not None

def group_tests_by_pattern(tests):
    """Group tests by token patterns"""
    grouped = {}
    unmatched = []
    
    for test in tests:
        matched = False
        for pattern in TOKEN_PATTERNS:
            if match_test_to_pattern(test, pattern):
                pattern_key = pattern.replace('*_', '').replace('*', '')
                if pattern_key not in grouped:
                    grouped[pattern_key] = []
                grouped[pattern_key].append(test)
                matched = True
                break
        
        if not matched:
            unmatched.append(test)
    
    # Add unmatched tests as individual groups
    for test in unmatched:
        grouped[test] = [test]
    
    return grouped

# ============================================================================
# SIDEBAR LAYOUT
# ============================================================================

sidebar = html.Div(
    [
        # Logo/Brand
        html.Div(
            [
                html.H4("Thermal", className="text-white mb-0"),
                html.H6("analysis", className="text-white-50 mt-0"),
            ],
            className="p-3 border-bottom border-secondary",
            style={'backgroundColor': COLORS['sidebar']}
        ),
        
        # Navigation Links
        html.Div(
            [
                dbc.Nav(
                    [
                        dbc.NavLink(
                            [html.I(className="fas fa-chart-line me-2"), "Dashboard"],
                            href="/",
                            id="nav-dashboard",
                            active="exact",
                            className="text-white"
                        ),
                        dbc.NavLink(
                            [html.I(className="fas fa-box me-2"), "Product"],
                            href="/product",
                            id="nav-product",
                            className="text-white"
                        ),
                        dbc.NavLink(
                            [html.I(className="fas fa-microscope me-2"), "Test Analysis"],
                            href="/test-analysis",
                            id="nav-test",
                            className="text-white"
                        ),
                        dbc.NavLink(
                            [html.I(className="fas fa-filter me-2"), "Data Filter"],
                            href="/data-filter",
                            id="nav-filter",
                            className="text-white"
                        ),
                        dbc.NavLink(
                            [html.I(className="fas fa-sliders-h me-2"), "Plot Setting"],
                            href="/plot-setting",
                            id="nav-plot",
                            className="text-white"
                        ),
                        dbc.NavLink(
                            [html.I(className="fas fa-calendar-week me-2"), "Profiles"],
                            href="/profiles",
                            id="nav-profiles",
                            className="text-white"
                        ),
                        html.Hr(className="border-secondary"),
                        dbc.NavLink(
                            [html.I(className="fas fa-cog me-2"), "Configuration"],
                            href="/configuration",
                            id="nav-config",
                            className="text-white"
                        ),
                        dbc.NavLink(
                            [html.I(className="fas fa-upload me-2"), "Data Upload"],
                            href="/data-upload",
                            id="nav-upload",
                            className="text-white"
                        ),
                    ],
                    vertical=True,
                    pills=True,
                ),
            ],
            className="p-3",
        ),
    ],
    id='dashboard-sidebar',
    style={
        'position': 'fixed',
        'top': 0,
        'left': 0,
        'bottom': 0,
        'width': '250px',
        'backgroundColor': COLORS['sidebar'],
        'overflowY': 'auto'
    }
)

# ============================================================================
# CONTENT LAYOUTS
# ============================================================================

def create_dashboard_content():
    """Dashboard main page - Summary of each test data against each product
    Uses preloaded data - guaranteed to be available at this point
    """
    if not data_store.loaded or data_store.df.empty:
        return html.Div([
            html.H3("📊 Dashboard"),
            html.Hr(),
            dbc.Alert([
                html.H5("📁 No Data Files Found"),
                html.P([
                    "The dashboard is looking for data files in the ",
                    html.Code("Input_data"),
                    " folder but none were found."
                ]),
                html.Br(),
                html.Strong("To get started:"),
                html.Ol([
                    html.Li(["Place Excel (", html.Code(".xlsx/.xls"), "), CSV (", html.Code(".csv"), "), or text (", html.Code(".txt"), ") files in the ", html.Code("Input_data"), " folder"]),
                    html.Li(["Files should contain columns: ", html.Code("TEST_NAME"), ", ", html.Code("TEST_RESULT"), ", ", html.Code("VISUAL_ID"), ", etc."]),
                    html.Li("Reload the dashboard or click 'Refresh Data' in Configuration tab")
                ]),
                html.Br(),
                html.P([
                    html.Strong("Current Status: "),
                    data_store.load_status
                ], className="text-muted"),
                html.P([
                    html.Strong("Data Folder: "),
                    html.Code(DATA_FOLDER)
                ], className="text-muted small")
            ], color="info", className="mt-3")
        ], className="p-4")
    
    df = data_store.df
    
    # Summary statistics - shown immediately (fast calculation from preloaded data)
    total_readings = data_store.overview['readings']
    total_products = data_store.overview['products']
    total_tests = data_store.overview['tests']
    total_units = data_store.overview['units']
    
    # Return structure with placeholders - data loaded progressively by callbacks
    return html.Div([
        # Header
        html.Div([
            html.H2("🌡️ Thermal Sensor Dashboard", className="mb-1"),
            html.P(f"Data loaded: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", 
                   className="text-muted mb-1"),
            html.P([
                "Data source: ",
                html.Strong(f"Profile {data_store.active_profile['work_week']}") if data_store.active_profile
                else html.Strong("Main dataset (input/)"),
            ], className="text-muted mb-3 small")
        ], className="mb-4"),
        
        # Summary Cards
        dbc.Row([
            dbc.Col(dbc.Card([
                dbc.CardBody([
                    html.H3(f"{total_readings:,}", className="text-primary mb-0"),
                    html.P("Total Readings", className="text-muted mb-0")
                ])
            ], className="text-center"), width=12, md=3),
            
            dbc.Col(dbc.Card([
                dbc.CardBody([
                    html.H3(f"{total_products}", className="text-success mb-0"),
                    html.P("Products", className="text-muted mb-0")
                ])
            ], className="text-center"), width=12, md=3),
            
            dbc.Col(dbc.Card([
                dbc.CardBody([
                    html.H3(f"{total_tests}", className="text-info mb-0"),
                    html.P("Tests", className="text-muted mb-0")
                ])
            ], className="text-center"), width=12, md=3),
            
            dbc.Col(dbc.Card([
                dbc.CardBody([
                    html.H3(f"{total_units:,}", className="text-warning mb-0"),
                    html.P("Units", className="text-muted mb-0")
                ])
            ], className="text-center"), width=12, md=3),
        ], className="mb-4"),
        
        # Product Cards - Progressive loading with spinner
        html.H4("📦 Products Overview", className="mb-3"),
        dcc.Loading(
            id="loading-product-cards",
            type="default",
            children=[
                html.Div(id='dashboard-product-cards', children=[
                    html.P("Loading product data...", className="text-muted")
                ])
            ]
        ),
        
        # Test vs Product Summary Matrix - Progressive loading with spinner
        html.Hr(),
        html.H4("📊 Test Summary by Product", className="mb-3 mt-4"),
        dcc.Loading(
            id="loading-test-matrix",
            type="default",
            children=[
                html.Div(id='test-product-matrix', children=[
                    html.P("Generating test comparison plots...", className="text-muted")
                ])
            ]
        ),
        
    ], className="p-4")

def create_product_content():
    """Product view - Select and analyze individual products with comprehensive filtering
    Uses preloaded and cached data for instant tab load
    """
    if not data_store.loaded or data_store.df.empty:
        return html.Div([html.H3("No Data Loaded")], className="p-4")
    
    # Use cached values for instant tab loading
    products = data_store.get_products_cached()
    product_options = [{'label': PRODUCT_DISPLAY_NAMES.get(p, p.replace('_', ' ')), 'value': p}
                       for p in products]
    
    unit_ids = data_store.get_unique_values_cached('Unit_ID')
    lots = data_store.get_unique_values_cached('Lot')
    programs = data_store.get_unique_values_cached('Program_Name')
    tests = data_store.get_unique_values_cached('Test')
    devrevs = data_store.get_unique_values_cached('DevRevStep')
    functional_bins = data_store.get_unique_values_cached('Functional_Bin')
    thermal_heads = data_store.get_unique_values_cached('Thermal_Head_Id')
    
    return html.Div([
        html.H2("📦 Product Analysis", className="mb-4"),
        
        # Main product selector and plot settings
        dbc.Row([
            dbc.Col([
                html.Label("Select Product:", className="fw-bold"),
                dcc.Dropdown(
                    id='product-selector',
                    options=product_options,
                    value=products[0] if products else None,
                    clearable=False,
                    className="mb-3"
                ),
            ], width=12, md=4),
            dbc.Col([
                html.Label("Individual Test Plots:", className="fw-bold"),
                dcc.RadioItems(
                    id='product-plot-count',
                    options=[
                        {'label': 'Show all (comprehensive)', 'value': 0},
                        {'label': 'First 5 (fast)', 'value': 5},
                        {'label': 'First 20', 'value': 20},
                    ],
                    value=0,
                    inline=True,
                    inputClassName="me-1",
                    labelClassName="me-3",
                ),
            ], width=12, md=8),
        ], className="mb-4"),
        
        # Comprehensive filtering section
        dbc.Card([
            dbc.CardHeader(html.H5("🔍 Advanced Filters", className="mb-0")),
            dbc.CardBody([
                dbc.Row([
                    dbc.Col([
                        html.Label("Unit ID:", className="small fw-bold"),
                        dcc.Dropdown(
                            id='product-unit-filter',
                            options=[{'label': u, 'value': u} for u in unit_ids],
                            multi=True,
                            placeholder=f"All units ({len(unit_ids)})"
                        ),
                    ], width=12, md=3, className="mb-3"),
                    
                    dbc.Col([
                        html.Label("Lot:", className="small fw-bold"),
                        dcc.Dropdown(
                            id='product-lot-filter',
                            options=[{'label': l, 'value': l} for l in lots],
                            multi=True,
                            placeholder=f"All lots ({len(lots)})"
                        ),
                    ], width=12, md=3, className="mb-3"),
                    
                    dbc.Col([
                        html.Label("Test Program:", className="small fw-bold"),
                        dcc.Dropdown(
                            id='product-program-filter',
                            options=[{'label': p, 'value': p} for p in programs],
                            multi=True,
                            placeholder=f"All programs ({len(programs)})"
                        ),
                    ], width=12, md=3, className="mb-3"),
                    
                    dbc.Col([
                        html.Label("Test Name:", className="small fw-bold"),
                        dcc.Dropdown(
                            id='product-test-filter',
                            options=[{'label': t, 'value': t} for t in tests],
                            multi=True,
                            placeholder=f"All tests ({len(tests)})"
                        ),
                    ], width=12, md=3, className="mb-3"),
                ]),
                
                dbc.Row([
                    dbc.Col([
                        html.Label("DevRevStep:", className="small fw-bold"),
                        dcc.Dropdown(
                            id='product-devrev-filter',
                            options=[{'label': d, 'value': d} for d in devrevs],
                            multi=True,
                            placeholder=f"All revisions ({len(devrevs)})"
                        ),
                    ], width=12, md=3, className="mb-3"),
                    
                    dbc.Col([
                        html.Label("Functional Bin:", className="small fw-bold"),
                        dcc.Dropdown(
                            id='product-bin-filter',
                            options=[{'label': b, 'value': b} for b in functional_bins],
                            multi=True,
                            placeholder=f"All bins ({len(functional_bins)})"
                        ),
                    ], width=12, md=3, className="mb-3"),
                    
                    dbc.Col([
                        html.Label("Thermal Head ID:", className="small fw-bold"),
                        dcc.Dropdown(
                            id='product-thermal-filter',
                            options=[{'label': t, 'value': t} for t in thermal_heads],
                            multi=True,
                            placeholder=f"All heads ({len(thermal_heads)})"
                        ),
                    ], width=12, md=6, className="mb-3"),
                ]),
                dbc.Row([
                    dbc.Col([html.Label('BOM:', className='small fw-bold'),
                             dcc.Dropdown(id='product-bom-filter', multi=True)], md=6),
                    dbc.Col([html.Label('PKG:', className='small fw-bold'),
                             dcc.Dropdown(id='product-pkg-filter', multi=True)], md=6),
                ]),
            ])
        ], className="mb-4"),
        
        # Export button
        dbc.Row([
            dbc.Col([
                dbc.Button(
                    [html.I(className="fas fa-file-pdf me-2"), "Export All Plots to PDF"],
                    id='product-export-pdf-btn',
                    color="success",
                    size="md",
                    className="me-2"
                ),
                dbc.Button(
                    [html.I(className="fas fa-image me-2"), "Export as Images"],
                    id='product-export-images-btn',
                    color="info",
                    size="md"
                ),
            ], md=6),
        ], className="mb-4"),
        html.Div(id='product-export-status', className="mb-3"),
        
        html.Div(id='product-details-content'),
        
    ], className="p-4")

def create_test_analysis_content():
    """Test analysis - Functional Bin commonality, repeat-failure check, and Test Program viewer
    Uses preloaded and cached data for instant tab load
    """
    if not data_store.loaded or data_store.df.empty:
        return html.Div([html.H3("No Data Loaded")], className="p-4")
    
    # Use cached values for instant tab loading
    products = data_store.get_products_cached()
    product_options = [{'label': 'All Products', 'value': '__ALL__'}] + [
        {'label': PRODUCT_DISPLAY_NAMES.get(p, p.replace('_', ' ')), 'value': p}
        for p in products
    ]
    
    lot_opts = [{'label': v, 'value': v} for v in data_store.get_unique_values_cached('Lot')]
    prog_opts = [{'label': v, 'value': v} for v in data_store.get_unique_values_cached('Program_Name')]
    unit_opts = [{'label': v, 'value': v} for v in data_store.get_unique_values_cached('Unit_ID')]
    test_opts = [{'label': v, 'value': v} for v in data_store.get_unique_values_cached('Test')]
    
    advanced_filters = dbc.Card([
        dbc.CardBody([
            html.Div([
                html.Strong("🔍 Advanced Filters", className="me-2"),
                html.Span("(apply to Lot Comparison, Bin Commonality and Unit Failure Repetition)",
                         className="text-muted small")
            ], className="mb-2"),
            dbc.Row([
                dbc.Col([
                    html.Label("Lot:", className="small fw-bold"),
                    dcc.Dropdown(id='ta-lot-filter', options=lot_opts, multi=True,
                                placeholder=f"All lots ({len(lot_opts)})"),
                ], md=3),
                dbc.Col([
                    html.Label("Test Program:", className="small fw-bold"),
                    dcc.Dropdown(id='ta-program-filter', options=prog_opts, multi=True,
                                placeholder=f"All programs ({len(prog_opts)})"),
                ], md=3),
                dbc.Col([
                    html.Label("VISUAL_ID:", className="small fw-bold"),
                    dcc.Dropdown(id='ta-visual-filter', options=unit_opts, multi=True,
                                placeholder=f"All units ({len(unit_opts)})"),
                ], md=3),
                dbc.Col([
                    html.Label("Test Name:", className="small fw-bold"),
                    dcc.Dropdown(id='ta-test-filter', options=test_opts, multi=True,
                                placeholder=f"All tests ({len(test_opts)})"),
                ], md=3),
            ]),
        ])
    ], className="mb-3")
    
    return html.Div([
        html.H2("🔬 Test Analysis", className="mb-3"),
        html.P("Investigate Functional Bin commonality, recurring unit failures, and Test Program configuration.",
               className="text-muted mb-3"),
        advanced_filters,
        
        dbc.Tabs(
            id="test-analysis-tabs",
            active_tab="tab-bin",
            children=[
                dbc.Tab(label='Lot Comparison', tab_id='tab-lot', children=html.Div([
                    dbc.Row([
                        dbc.Col([html.Label('Product:', className='fw-bold'),
                                 dcc.Dropdown(id='compare-product', options=product_options,
                                              value='__ALL__', clearable=False)], md=4),
                        dbc.Col([html.Label('BOM:', className='fw-bold'),
                                 dcc.Dropdown(id='compare-bom', multi=True)], md=4),
                        dbc.Col([html.Label('PKG:', className='fw-bold'),
                                 dcc.Dropdown(id='compare-pkg', multi=True)], md=4),
                    ], className='mb-3'),
                    dbc.Row([
                        dbc.Col([html.Label('Test Name:', className='fw-bold'),
                                 dcc.Dropdown(id='compare-test', clearable=True)], md=6),
                        dbc.Col([html.Label('Sensor:', className='fw-bold'),
                                 dcc.Dropdown(id='compare-sensor', multi=True)], md=6),
                    ], className='mb-3'),
                    dcc.Loading(html.Div([
                        dcc.Graph(id='lot-comparison-chart'),
                        html.Div(id='lot-comparison-status', className='text-muted small mb-2'),
                        dash_table.DataTable(id='lot-comparison-table',
                            columns=[{'name': column, 'id': column} for column in
                                     ['Product_Name', 'BOM', 'PKG', 'Test', 'Measurement_Point',
                                      'Lot', 'readings', 'mean', 'stddev', 'minimum', 'maximum']],
                            page_action='custom', page_current=0, page_size=50,
                            sort_action='custom', sort_mode='multi', sort_by=[],
                            style_table={'overflowX': 'auto'},
                            style_cell={'fontFamily': 'inherit', 'textAlign': 'left',
                                        'maxWidth': '240px', 'whiteSpace': 'normal'}),
                    ])),
                ], className='p-3')),
                dbc.Tab(label="Bin Commonality", tab_id="tab-bin", children=html.Div([
                    dbc.Row([
                        dbc.Col([
                            html.Label("Product:", className="fw-bold"),
                            dcc.Dropdown(
                                id='bin-product-selector',
                                options=product_options,
                                value='__ALL__',
                                clearable=False
                            ),
                        ], md=4),
                        dbc.Col([
                            html.Label("Functional Bin:", className="fw-bold"),
                            dcc.Dropdown(
                                id='bin-fbin-selector',
                                clearable=False,
                                placeholder="Select Functional Bin"
                            ),
                        ], md=4),
                        dbc.Col([
                            html.Label("Commonality Parameter:", className="fw-bold"),
                            dcc.Dropdown(
                                id='bin-param-selector',
                                options=[
                                    {'label': 'Program Name', 'value': 'Program_Name'},
                                    {'label': 'Thermal Head Id', 'value': 'Thermal_Head_Id'},
                                    {'label': 'Unit Tester Site Id', 'value': 'Unit_Tester_Site_Id'},
                                    {'label': 'Unit TIU', 'value': 'Unit_TIU'},
                                    {'label': 'Lot', 'value': 'Lot'},
                                ],
                                value='Program_Name',
                                clearable=False
                            ),
                        ], md=4),
                    ], className="mb-3 mt-3"),
                    html.Div(id='bin-commonality-output'),
                ], className="p-3")),
                
                dbc.Tab(label="Unit Failure Repetition", tab_id="tab-repeat", children=html.Div([
                    dbc.Row([
                        dbc.Col([
                            html.Label("Product:", className="fw-bold"),
                            dcc.Dropdown(
                                id='repeat-product-selector',
                                options=product_options,
                                value='__ALL__',
                                clearable=False
                            ),
                        ], md=4),
                        dbc.Col([
                            html.Label("Failure Bin Filter:", className="fw-bold"),
                            dcc.Dropdown(
                                id='repeat-bin-selector',
                                clearable=True,
                                placeholder="(Optional) Restrict to specific Functional Bin"
                            ),
                        ], md=4),
                        dbc.Col([
                            html.Label("Min Failure Count:", className="fw-bold"),
                            dcc.Input(
                                id='repeat-min-count', type='number', value=2, min=1, step=1,
                                className="form-control"
                            ),
                        ], md=4),
                    ], className="mb-3 mt-3"),
                    html.Div(id='repeat-failure-output'),
                ], className="p-3")),
                
                dbc.Tab(label="Test Program Config", tab_id="tab-program", children=html.Div([
                    dbc.Row([
                        dbc.Col([
                            html.Label("Module:", className="fw-bold"),
                            dcc.Dropdown(
                                id='prog-module-selector',
                                options=[{'label': m, 'value': m} for m in PROGRAM_MODULES],
                                value=PROGRAM_MODULES[0],
                                clearable=False
                            ),
                        ], md=4),
                        dbc.Col([
                            html.Label("Program (auto-scanned):", className="fw-bold"),
                            dcc.Dropdown(
                                id='prog-name-selector',
                                clearable=False,
                                placeholder="Scanning network shares..."
                            ),
                        ], md=5),
                        dbc.Col([
                            html.Label(" ", className="fw-bold d-block"),
                            dbc.Button("🔄 Rescan", id='prog-rescan-btn', color="secondary", size="sm"),
                        ], md=3),
                    ], className="mb-3 mt-3"),
                    dbc.Alert(
                        [html.Strong("Source paths: "), html.Br(),
                         *[html.Span([p, html.Br()]) for p in PROGRAM_DIRS]],
                        color="light", className="small"
                    ),
                    html.Div(id='prog-config-output'),
                ], className="p-3")),
            ]
        ),
    ], className="p-4")

def create_data_filter_content():
    """Data filter controls"""
    if not data_store.load_data() or data_store.df.empty:
        return html.Div([html.H3("No Data Loaded")], className="p-4")
    
    df = data_store.df
    
    return html.Div([
        html.H2("🔍 Data Filter", className="mb-4"),
        
        dbc.Row([
            dbc.Col([
                html.Label("Products:", className="fw-bold"),
                dcc.Checklist(
                    id='filter-products',
                    options=[{'label': PRODUCT_DISPLAY_NAMES.get(p, p.replace('_', ' ')), 'value': p}
                                for p in data_store.get_products_cached()],
                            value=data_store.get_products_cached(),
                    className="mb-3"
                ),
            ], width=12, md=4),
            
            dbc.Col([
                html.Label("Tests:", className="fw-bold"),
                dcc.Checklist(
                    id='filter-tests',
                    options=[{'label': t, 'value': t} for t in data_store.backend.values('Test')],
                    value=data_store.backend.values('Test')[:5],
                    className="mb-3",
                    style={'maxHeight': '400px', 'overflowY': 'auto'}
                ),
            ], width=12, md=8),
        ]),
        
        dbc.Button("Apply Filters", id="apply-filters-btn", color="primary", className="me-2"),
        dbc.Button("Reset Filters", id="reset-filters-btn", color="secondary"),
        html.Div(id="filter-status", className="mt-3"),
        
    ], className="p-4")

def create_plot_setting_content():
    """Plot settings controls"""
    return html.Div([
        html.H2("📊 Plot Settings", className="mb-4"),
        
        dbc.Row([
            dbc.Col([
                html.Label("Plot Type:", className="fw-bold"),
                dcc.RadioItems(
                    id='plot-type',
                    options=[
                        {'label': 'Scatter', 'value': 'scatter'},
                        {'label': 'Box', 'value': 'box'},
                        {'label': 'Histogram', 'value': 'histogram'},
                    ],
                    value='scatter',
                    className="mb-3"
                ),
            ], width=12, md=4),
            
            dbc.Col([
                html.Label("X-Axis:", className="fw-bold"),
                dcc.RadioItems(
                    id='x-axis-type',
                    options=[
                        {'label': 'Sensor', 'value': 'sensor'},
                        {'label': 'Unit ID', 'value': 'unit'},
                        {'label': 'Lot', 'value': 'lot'},
                    ],
                    value='sensor',
                    className="mb-3"
                ),
            ], width=12, md=4),
            
            dbc.Col([
                html.Label("Sigma Level:", className="fw-bold"),
                dcc.Slider(
                    id='sigma-level',
                    min=3,
                    max=10,
                    step=1,
                    value=10,
                    marks={i: f'{i}σ' for i in range(3, 11)},
                    className="mb-3"
                ),
            ], width=12, md=4),
        ]),
        
        dbc.Row([
            dbc.Col([
                dbc.Checkbox(
                    id='show-control-limits',
                    label="Show Control Limits",
                    value=True,
                    className="mb-2"
                ),
                dbc.Checkbox(
                    id='show-mean-line',
                    label="Show Mean Line",
                    value=True,
                    className="mb-2"
                ),
                dbc.Checkbox(
                    id='show-legend',
                    label="Show Legend",
                    value=True,
                    className="mb-2"
                ),
            ], width=12, md=6),
        ]),
        
    ], className="p-4")

def create_configuration_content():
    """Configuration management - uses preloaded config data"""
    if not data_store.loaded:
        return html.Div([html.H3("No Configuration Loaded")], className="p-4")
    
    config_data = data_store.config_data or {}
    
    # Check cache status
    cache_valid = is_data_cache_valid()
    cache_status_color = "success" if cache_valid else "warning"
    cache_status_text = "✅ Cache is valid and current" if cache_valid else "⚠️ Cache is outdated or missing"
    
    return html.Div([
        html.H2("⚙️ Configuration", className="mb-4"),
        
        # Data Cache Management
        dbc.Card([
            dbc.CardBody([
                html.H5("💾 Data Cache Management", className="mb-3"),
                html.P(
                    "The dashboard saves processed data to speed up future launches. "
                    "When you update input data, the cache is automatically invalidated.",
                    className="text-muted small mb-3"
                ),
                dbc.Alert(cache_status_text, color=cache_status_color, className="mb-3"),
                dbc.Button([html.I(className="fas fa-trash-alt me-2"), "Clear Cache"],
                          id="config-clear-cache-btn", color="danger", size="sm"),
                html.Small("Click to force reload from source files on next launch", 
                          className="text-muted d-block mt-2"),
                html.Div(id='cache-clear-result', className="mt-3"),
            ])
        ], className="mb-4"),
        
        # Configuration Management - Reload config
        dbc.Card([
            dbc.CardBody([
                html.H5("🔄 Configuration Management", className="mb-3"),
                html.P(
                    "Reload the configuration files from the Config folder. "
                    "Use this if you've updated any .json config files.",
                    className="text-muted small mb-3"
                ),
                dbc.Button([html.I(className="fas fa-sync-alt me-2"), "Refresh Configuration"],
                          id="config-refresh-btn", color="info", size="sm"),
                html.Small("Click to reload config files without restarting the dashboard", 
                          className="text-muted d-block mt-2"),
                html.Div(id='config-refresh-result', className="mt-3"),
            ])
        ], className="mb-4"),
        
        # Sigma level control - applies globally to all plots/statistics
        dbc.Card([
            dbc.CardBody([
                html.H5("📐 Sigma Level for Control Limits", className="mb-3"),
                html.P(
                    "Sets the sigma multiplier used to compute UCL = mean + Nσ and LCL = mean - Nσ. "
                    "This value applies to all dashboard plots and statistics tables.",
                    className="text-muted small mb-3"
                ),
                dcc.Slider(
                    id='config-sigma-slider',
                    min=1, max=10, step=1,
                    value=SIGMA_LEVEL,
                    marks={i: f'{i}σ' for i in range(1, 11)},
                ),
                html.Div(id='config-sigma-display', className="mt-2 text-primary fw-bold"),
            ])
        ], className="mb-4"),
        
        html.Div([
            html.H5(f"Loaded Configurations: {len(config_data)}"),
            html.P(f"Config Folder: {CONFIG_FOLDER}", className="text-muted"),
        ], className="mb-4"),
        
        dbc.Table([
            html.Thead(html.Tr([
                html.Th("Test Name"),
                html.Th("Sensors"),
                html.Th("Y-Axis Range"),
            ])),
            html.Tbody([
                html.Tr([
                    html.Td(test_name, style={'fontSize': '0.85em'}),
                    html.Td(len(config.get('COLUMN_NAMES', []))),
                    html.Td(f"{config.get('Y_AXIS_MIN', 'N/A')} - {config.get('Y_AXIS_MAX', 'N/A')}"),
                ])
                for test_name, config in list(config_data.items())[:20]  # Show first 20
            ])
        ], striped=True, bordered=True, hover=True, size="sm"),
        
        html.P(f"Showing first 20 of {len(config_data)} configurations", 
               className="text-muted mt-2"),
        
    ], className="p-4", style={'maxHeight': '600px', 'overflowY': 'auto'})

def create_data_upload_content():
    """Data upload interface"""
    return html.Div([
        html.H2("📤 Data Upload", className="mb-4"),
        
        dbc.Alert([
            html.H5("Upload Instructions", className="alert-heading"),
            html.P([
                "1. Place data files in the ", html.Code(DATA_FOLDER), " folder", html.Br(),
                "2. Supported formats: Excel (.xlsx, .xls), CSV (.csv), Text (.txt)", html.Br(),
                "3. Click 'Refresh Data' to reload", html.Br(),
                "4. Required columns: TEST_NAME, DISTINCTIVE_VALUE, TEST_RESULT_NUMERIC, VISUAL_ID, Lot"
            ])
        ], color="info", className="mb-4"),
        
        html.Div([
            html.H5("Current Data Folder Status:"),
            html.P(f"Path: {os.path.abspath(DATA_FOLDER)}", className="mb-2"),
            html.P(f"Files found: {len(glob.glob(os.path.join(DATA_FOLDER, '*.*')))}", className="mb-2"),
        ], className="mb-4"),
        
        dbc.Button("🔄 Refresh Data", id="refresh-data-btn", color="primary", size="lg"),
        dbc.Button("🗑️ Clear Cache & Reset", id="clear-cache-btn", color="danger",
                   size="lg", className="ms-2"),
        html.Div(id="refresh-status", className="mt-3"),
        
    ], className="p-4")

def _render_profiles_table(active_slug=None):
    """Build the profiles table shown on the Profiles page, re-rendered after every action."""
    rows = profile_store.list_profiles()
    if not rows:
        return dbc.Alert("No profiles yet. Create one above to start pulling a work week's data.", color="info")
    header = html.Thead(html.Tr([
        html.Th("Work Week"), html.Th("Product"), html.Th("Test Program"),
        html.Th("Lots"), html.Th("Status"), html.Th("Updated"), html.Th("Actions"),
    ]))
    body_rows = []
    for profile in rows:
        slug = profile['slug']
        is_active = slug == active_slug
        body_rows.append(html.Tr([
            html.Td([
                profile['work_week'],
                html.Span(" (active)", className="text-success fw-bold") if is_active else "",
            ]),
            html.Td(PRODUCT_DISPLAY_NAMES.get(profile.get('product'), profile.get('product')) or "—"),
            html.Td(profile.get('test_program') or "—"),
            html.Td(", ".join(profile.get('lots') or []) or "—"),
            html.Td(profile.get('last_pull_status', 'Never pulled'), className="small"),
            html.Td((profile.get('updated') or '')[:19], className="small text-muted"),
            html.Td([
                dbc.Button("Open", id={'type': 'profile-open', 'index': slug},
                           color="primary", size="sm", className="me-1", disabled=is_active),
                dbc.Button("Pull/Refresh", id={'type': 'profile-refresh', 'index': slug},
                           color="secondary", size="sm", className="me-1"),
                dbc.Button("Delete", id={'type': 'profile-delete', 'index': slug},
                           color="danger", size="sm"),
            ]),
        ]))
    return dbc.Table([header, html.Tbody(body_rows)], striped=True, bordered=True, hover=True, size="sm")

def _pull_and_crunch_profile(profile):
    """Pull a profile's data via Aqua/Trace (if configured) and refresh its isolated DuckDB cache."""
    slug = profile['slug']
    try:
        written = profile_store.pull_profile_data(profile)
        pull_note = f"Pulled {len(written)} file(s) from Aqua/Trace." if written else "Aqua/Trace returned no files."
    except profile_store.AquaTraceNotConfigured as e:
        pull_note = str(e)
    except Exception as e:
        profile_store.mark_pull_result(slug, f"Pull failed: {e}")
        return dbc.Alert(f"Data pull failed for '{slug}': {e}", color="danger", dismissable=True)

    folder = profile_store.profile_input_folder(slug)
    is_active = bool(data_store.active_profile and data_store.active_profile.get('slug') == slug)
    try:
        if is_active:
            # Reuse the already-open connection instead of opening the same database twice.
            data_store.loaded = False
            data_store.load_data()
            readings = data_store.overview.get('readings', 0) if data_store.loaded else 0
        else:
            store = ThermalStore(profile_store.profile_database_path(slug))
            try:
                store.refresh(folder, load_config_from_folder(), PRODUCT_MAPPING)
                readings = store.overview().get('readings', 0)
            finally:
                store.close()
        status = f"{pull_note} Crunched {readings:,} readings from {folder}."
        profile_store.mark_pull_result(slug, status)
        return dbc.Alert(status, color="success", dismissable=True)
    except Exception as e:
        status = f"{pull_note} Crunch failed: {e}"
        profile_store.mark_pull_result(slug, status)
        return dbc.Alert(status, color="warning", dismissable=True)

def create_profiles_content():
    """Work-week profiles: save Aqua/Trace query parameters and crunch each week's data in its own cache."""
    active_slug = data_store.active_profile.get('slug') if data_store.active_profile else None
    product_options = [{'label': PRODUCT_DISPLAY_NAMES.get(p, p), 'value': p}
                        for p in sorted(set(PRODUCT_MAPPING.values()))]
    return html.Div([
        html.H2("📅 Work Week Profiles", className="mb-1"),
        html.P("Save a profile per work week with its lot(s) and test program, then pull and crunch "
               "that week's data in its own isolated cache without touching the main dataset.",
               className="text-muted mb-4"),

        dbc.Card([
            dbc.CardBody([
                html.H5("Create Profile", className="mb-3"),
                dbc.Row([
                    dbc.Col([
                        dbc.Label("Work Week"),
                        dbc.Input(id="profile-work-week", placeholder="WW46", type="text"),
                        html.Small("Unique ID for this profile, e.g. WW46 or WW46-N2P",
                                   className="text-muted"),
                    ], md=2),
                    dbc.Col([dbc.Label("Product"),
                             dcc.Dropdown(id="profile-product", options=product_options,
                                          placeholder="Select product")], md=3),
                    dbc.Col([dbc.Label("Test Program"),
                             dbc.Input(id="profile-test-program", placeholder="e.g. 1278xxx", type="text")], md=3),
                    dbc.Col([dbc.Label("Lot(s)"),
                             dbc.Input(id="profile-lots", placeholder="Comma or space separated lot IDs",
                                       type="text")], md=4),
                ], className="g-2 mb-3"),
                dbc.Row([
                    dbc.Col([dbc.Label("Notes (optional)"), dbc.Textarea(id="profile-notes")], md=12),
                ], className="g-2 mb-3"),
                dbc.Button([html.I(className="fas fa-plus me-2"), "Create Profile & Pull Data"],
                           id="profile-create-btn", color="primary"),
                html.Div(id="profile-form-status", className="mt-3"),
            ])
        ], className="mb-4"),

        html.H5("Saved Profiles", className="mb-3"),
        html.Div(id="profile-action-status"),
        html.Div(id="profiles-table-container", children=_render_profiles_table(active_slug)),

        html.Hr(),
        dbc.Button("Switch to Main Dataset", id="profile-switch-main-btn", color="secondary",
                   size="sm", disabled=active_slug is None),

        dbc.Alert([
            html.Strong("Aqua/Trace connection: "),
            "not yet configured. Creating or refreshing a profile reserves its folder under ",
            html.Code(str(profile_store.PROFILE_INPUT_ROOT) + "/<work week>"),
            " — drop exported CSV/TXT/XLSX files there and click Pull/Refresh, or wire up "
            "profiles.AQUA_TRACE_FETCHER to automate the pull for lot/test-program inputs.",
        ], color="warning", className="mt-4"),

    ], className="p-4")

# ============================================================================
# MAIN LAYOUT
# ============================================================================

app.layout = html.Div([
    dcc.Location(id='url', refresh=False),
    dcc.Store(id='sigma-store', data=SIGMA_LEVEL, storage_type='session'),
    dcc.Store(id='filter-store', data={}, storage_type='session'),
    dcc.Store(id='active-profile-store', data=None, storage_type='session'),
    html.Div(id='active-profile-banner', style={
        'position': 'fixed', 'top': '10px', 'right': '20px', 'zIndex': 1000,
        'borderRadius': '4px', 'overflow': 'hidden'
    }),
    sidebar,
    html.Div(
        id='page-content',
        style={
            'marginLeft': '250px',
            'padding': '0',
            'backgroundColor': COLORS['light_bg'],
            'minHeight': '100vh'
        }
    )
])

# ============================================================================
# CALLBACKS
# ============================================================================

def _query_filters(filter_data=None, **selections):
    filters = {}
    for key, column in [('products', 'Product_Name'), ('tests', 'Test')]:
        if (filter_data or {}).get(key):
            filters[column] = filter_data[key]
    for column, values in selections.items():
        if values is None or values == [] or values == '__ALL__':
            continue
        values = values if isinstance(values, list) else [values]
        if column in filters:
            values = [value for value in values if value in filters[column]]
            if not values:
                values = ['\x00no-matching-filter']
        filters[column] = values
    return filters


def _product_filters(filter_data, product, units, lots, programs, tests, devrevs, bins, heads, bom=None, pkg=None):
    return _query_filters(filter_data, Product_Name=product, Unit_ID=units, Lot=lots,
                          Program_Name=programs, Test=tests, DevRevStep=devrevs,
                          Functional_Bin=bins, Thermal_Head_Id=heads, BOM=bom, PKG=pkg)


def _register_search_options(component_id, column, product_id=None):
    dependencies = [Input(component_id, 'search_value')]
    if product_id:
        dependencies.append(Input(product_id, 'value'))

    @app.callback(Output(component_id, 'options'), *dependencies, State(component_id, 'value'))
    def search_options(*arguments):
        if not data_store.loaded:
            return []
        search = arguments[0]
        selected = arguments[-1]
        filters = _query_filters(Product_Name=arguments[1]) if product_id else {}
        return data_store.backend.options(column, filters, search, selected)


for _component, _column in [('unit', 'Unit_ID'), ('lot', 'Lot'), ('program', 'Program_Name'),
                            ('test', 'Test'), ('devrev', 'DevRevStep'), ('bin', 'Functional_Bin'),
                            ('thermal', 'Thermal_Head_Id'), ('bom', 'BOM'), ('pkg', 'PKG')]:
    _register_search_options(f'product-{_component}-filter', _column, 'product-selector')
for _component, _column in [('lot', 'Lot'), ('program', 'Program_Name'), ('visual', 'Unit_ID'), ('test', 'Test')]:
    _register_search_options(f'ta-{_component}-filter', _column)
for _component, _column in [('bom', 'BOM'), ('pkg', 'PKG'), ('test', 'Test'), ('sensor', 'Measurement_Point')]:
    _register_search_options(f'compare-{_component}', _column, 'compare-product')


@app.callback(
    Output('lot-comparison-table', 'page_current'),
    Input('compare-product', 'value'), Input('compare-bom', 'value'), Input('compare-pkg', 'value'),
    Input('compare-test', 'value'), Input('compare-sensor', 'value'),
    Input('ta-lot-filter', 'value'), Input('ta-program-filter', 'value'),
    Input('ta-visual-filter', 'value'), Input('ta-test-filter', 'value'), Input('filter-store', 'data'),
)
def reset_lot_page(*selections):
    return 0


@app.callback(
    Output('lot-comparison-table', 'data'), Output('lot-comparison-table', 'page_count'),
    Output('lot-comparison-chart', 'figure'), Output('lot-comparison-status', 'children'),
    Input('compare-product', 'value'), Input('compare-bom', 'value'), Input('compare-pkg', 'value'),
    Input('compare-test', 'value'), Input('compare-sensor', 'value'),
    Input('ta-lot-filter', 'value'), Input('ta-program-filter', 'value'),
    Input('ta-visual-filter', 'value'), Input('ta-test-filter', 'value'), Input('filter-store', 'data'),
    Input('lot-comparison-table', 'page_current'), Input('lot-comparison-table', 'sort_by'),
)
def update_lot_comparison(product, bom, pkg, test, sensors, lots, programs, units,
                          tests, filter_data, page, sort_by):
    figure = go.Figure()
    figure.update_layout(template='plotly_white', height=500, xaxis_title='Lot',
                          yaxis_title='Mean +/- population standard deviation',
                          xaxis_type='category', margin=dict(t=50, b=100),
                          legend=dict(orientation='h', y=-0.3))
    if not data_store.loaded or not test:
        return [], 1, figure, 'Select a test name.'
    filters = _query_filters(filter_data, Product_Name=product, BOM=bom, PKG=pkg, Test=tests,
                             Measurement_Point=sensors, Lot=lots, Program_Name=programs, Unit_ID=units)
    if 'Test' in filters and test not in filters['Test']:
        return [], 1, figure, 'No matching readings.'
    filters['Test'] = test
    rows, pages = data_store.backend.statistics(filters, page=page or 0, sort_by=sort_by, by_lot=True)
    if not rows:
        return [], pages, figure, 'No matching readings.'
    frame = pd.DataFrame(rows)
    for identity, group in frame.groupby(['Product_Name', 'BOM', 'PKG', 'Test', 'Measurement_Point'], dropna=False):
        figure.add_trace(go.Scatter(
            x=group['Lot'].replace('', '(missing)'), y=group['mean'], mode='markers',
            name=' | '.join(value or '(missing)' for value in [identity[0], identity[1], identity[2], identity[4]]),
            error_y=dict(type='data', array=group['stddev'], visible=True),
            customdata=group[['readings', 'minimum', 'maximum']].to_numpy(),
            hovertemplate='Lot: %{x}<br>Mean: %{y:.6g}<br>Readings: %{customdata[0]}'
                          '<br>Min: %{customdata[1]:.6g}<br>Max: %{customdata[2]:.6g}<extra>%{fullData.name}</extra>',
        ))
    figure.update_layout(title=dict(text=test, font=dict(size=14)))
    missing = [column for column in ['BOM', 'PKG', 'Lot'] if (frame[column] == '').any()]
    status = f'Exact full-data statistics. Page {(page or 0) + 1} of {pages}; {len(rows)} groups plotted.'
    if missing:
        status += ' Missing source metadata: ' + ', '.join(missing) + '.'
    return rows, pages, figure, status


def _apply_filter(df, filt):
    """Apply the active Data Filter (products, tests) to a DataFrame.
    Uses memory-efficient filtering for large datasets via .isin() and chunking.
    """
    if df is None or df.empty or not filt:
        return df
    
    products = filt.get('products') or []
    tests = filt.get('tests') or []
    
    if products and 'Product_Name' in df.columns:
        # Use isin() which is more memory-efficient than == for large datasets
        df = df[df['Product_Name'].isin(products)]
    
    if tests and 'Test' in df.columns:
        # Use isin() which is more memory-efficient than == for large datasets
        df = df[df['Test'].isin(tests)]
    
    return df


def _apply_ta_filter(df, lots, programs, units, tests=None):
    """Apply Test Analysis advanced filters (Lot / Program / VISUAL_ID / Test).
    Each list arg is a multi-select value; None or [] means no restriction.
    Uses memory-efficient filtering for large datasets.
    """
    if df is None or df.empty:
        return df
    
    if lots and 'Lot' in df.columns:
        # Convert lots to strings once
        lots_str = [str(x) for x in lots]
        df = df[df['Lot'].astype(str).isin(lots_str)]
    
    if programs and 'Program_Name' in df.columns:
        # Convert programs to strings once
        programs_str = [str(x) for x in programs]
        df = df[df['Program_Name'].astype(str).isin(programs_str)]
    
    if units and 'Unit_ID' in df.columns:
        # Convert units to strings once
        units_str = [str(x) for x in units]
        df = df[df['Unit_ID'].astype(str).isin(units_str)]
    
    if tests and 'Test' in df.columns:
        # Convert tests to strings once
        tests_str = [str(x) for x in tests]
        df = df[df['Test'].astype(str).isin(tests_str)]
    
    return df


@app.callback(
    Output('filter-store', 'data'),
    Output('filter-status', 'children'),
    Input('apply-filters-btn', 'n_clicks'),
    Input('reset-filters-btn', 'n_clicks'),
    State('filter-products', 'value'),
    State('filter-tests', 'value'),
    prevent_initial_call=True,
)
def update_filter_store(apply_clicks, reset_clicks, products, tests):
    """Persist the Data Filter selection so charts can react to Apply Filters."""
    triggered = callback_context.triggered[0]['prop_id'] if callback_context.triggered else ''
    if 'reset-filters-btn' in triggered:
        return {}, dbc.Alert("Filters reset. All data is now visible.", color="secondary", dismissable=True)
    filt = {'products': products or [], 'tests': tests or []}
    msg = dbc.Alert(
        f"✅ Filters applied: {len(filt['products'])} product(s), {len(filt['tests'])} test(s). "
        "Charts on Dashboard, Product and Test Analysis will reflect this.",
        color="success", dismissable=True
    )
    return filt, msg


@app.callback(
    Output('page-content', 'children'),
    Input('url', 'pathname')
)
def display_page(pathname):
    """Route to different pages"""
    if pathname == '/product':
        return create_product_content()
    elif pathname == '/test-analysis':
        return create_test_analysis_content()
    elif pathname == '/data-filter':
        return create_data_filter_content()
    elif pathname == '/plot-setting':
        return create_plot_setting_content()
    elif pathname == '/profiles':
        return create_profiles_content()
    elif pathname == '/configuration':
        return create_configuration_content()
    elif pathname == '/data-upload':
        return create_data_upload_content()
    else:  # Default to dashboard
        return create_dashboard_content()

@app.callback(
    Output('test-product-matrix', 'children'),
    Input('url', 'pathname'),
    Input('filter-store', 'data')
)
def update_test_product_matrix(pathname, filter_data):
    """Generate test comparison plots grouped by token patterns across products"""
    if pathname != '/' or not data_store.loaded or data_store.df.empty:
        return html.Div()
    
    filters = _query_filters(filter_data)
    if not data_store.backend.overview(filters)['readings']:
        return dbc.Alert("No data matches the active filters.", color="warning", className="m-3")
    
    # Use cached values for fast homepage load
    products = data_store.backend.values('Product_Name', filters)
    test_groups = data_store.get_test_groups_cached()
    
    # Get total unique tests from filtered data
    tests = data_store.backend.values('Test', filters)
    
    # Create comparison plots for each test group
    plot_divs = []
    
    for group_name, group_tests in sorted(test_groups.items()):
        # Filter data for this group of tests
        selected_tests = [test for test in group_tests if test in tests]
        if not selected_tests:
            continue
        group_df = data_store.backend.boxes(dict(filters, Test=selected_tests))
        
        if group_df.empty:
            continue
        
        # Create comparison plot across products
        fig = go.Figure()
        
        # Color palette for products
        color_palette = px.colors.qualitative.Set2
        
        # Determine sensor order (consistent across products)
        # Use notna() instead of dropna() to avoid memory issues on large dataframes
        sensor_order = sorted(group_df['Measurement_Point'][group_df['Measurement_Point'].notna()].unique())
        
        for idx, product in enumerate(products):
            product_data = group_df[group_df['Product_Name'] == product]
            
            if product_data.empty:
                continue
            
            # Get display name
            display_name = PRODUCT_DISPLAY_NAMES.get(product, product.replace('_', ' '))
            color = color_palette[idx % len(color_palette)]
            
            # Box plot per sensor, side-by-side per product (grouped boxmode)
            # Use safe string encoding for hovertemplate to avoid Unicode errors
            safe_display_name = str(display_name).encode('utf-8', errors='ignore').decode('utf-8', errors='ignore')
            fig.add_trace(go.Box(
                x=product_data['Measurement_Point'],
                q1=product_data['q1'], median=product_data['median'], q3=product_data['q3'],
                lowerfence=product_data['minimum'], upperfence=product_data['maximum'],
                mean=product_data['mean'], sd=product_data['sd'],
                name=safe_display_name,
                marker=dict(color=color, size=3, opacity=0.5),
                line=dict(color=color, width=1.5),
                fillcolor=color,
                opacity=0.65,
                boxpoints=False,
                boxmean=True,
                hovertemplate=(
                    f'<b>{safe_display_name}</b><br>'
                    'Sensor: %{x}<br>'
                    'Value: %{y:.3f}<extra></extra>'
                )
            ))
        
        # Update layout
        title = f"Test Group: {group_name}"
        if len(group_tests) > 1:
            title += f" ({len(group_tests)} tests)"
        else:
            title = f"Test: {group_tests[0]}"
        
        fig.update_layout(
            title=title,
            xaxis_title="Sensor / Measurement Point",
            yaxis_title="Test Result Value",
            height=550,
            hovermode='closest',
            showlegend=True,
            boxmode='group',
            boxgap=0.2,
            boxgroupgap=0.1,
            legend=dict(
                orientation="h",
                yanchor="bottom",
                y=1.02,
                xanchor="left",
                x=0
            ),
            xaxis=dict(
                tickangle=-45,
                type='category',
                categoryorder='array',
                categoryarray=sensor_order
            ),
            margin=dict(t=80, b=120),
            plot_bgcolor='#fafafa'
        )
        
        # Create card with plot
        card = dbc.Card([
            dbc.CardBody([
                dcc.Graph(figure=fig, config={'displayModeBar': True}),
                html.P([
                    html.Strong("Tests in group: "),
                    html.Br(),
                    ', '.join([t[:60] + ('...' if len(t) > 60 else '') for t in group_tests[:5]]),
                    html.Br() if len(group_tests) > 5 else '',
                    f"... and {len(group_tests) - 5} more tests" if len(group_tests) > 5 else ''
                ], className="text-muted small mt-2")
            ])
        ], className="mb-4")
        
        plot_divs.append(card)
    
    # Return all plots
    return html.Div([
        html.P([
            html.Strong(f"Showing {len(plot_divs)} test groups "),
            f"from {len(tests)} total tests. ",
            f"Tests are grouped by token patterns for comparison across {len(products)} products."
        ], className="text-info mb-3"),
        html.Div(plot_divs)
    ])

@app.callback(
    Output('dashboard-product-cards', 'children'),
    Input('url', 'pathname'),
    Input('filter-store', 'data')
)
def update_dashboard_product_cards(pathname, filter_data):
    """Generate product cards progressively - shows as data completes
    This callback updates independently so cards appear while test matrix is still loading
    """
    if pathname != '/' or not data_store.load_data() or data_store.df.empty:
        return html.Div()
    
    filters = _query_filters(filter_data)
    if not data_store.backend.overview(filters)['readings']:
        return dbc.Alert("No data matches the active filters.", color="warning", className="m-3")
    
    # Generate product summary cards
    product_cards = []
    for product in data_store.backend.values('Product_Name', filters):
        counts = data_store.backend.overview(dict(filters, Product_Name=product))
        display_name = PRODUCT_DISPLAY_NAMES.get(product, product.replace('_', ' '))
        
        card = dbc.Card([
            dbc.CardBody([
                html.H5(display_name, className="card-title"),
                html.P([
                    html.Strong("Tests: "), f"{counts['tests']}", html.Br(),
                    html.Strong("Units: "), f"{counts['units']}", html.Br(),
                    html.Strong("Readings: "), f"{counts['readings']:,}", html.Br(),
                    html.Strong("Lots: "), f"{counts['lots']}"
                ], className="card-text mb-0")
            ])
        ], className="mb-3", color="light")
        product_cards.append(dbc.Col(card, width=12, lg=6, xl=4))
    
    return dbc.Row(product_cards) if product_cards else html.Div("No products found.")

@app.callback(
    Output('sigma-store', 'data', allow_duplicate=True),
    Output('config-sigma-display', 'children'),
    Input('config-sigma-slider', 'value'),
    prevent_initial_call=True
)
def update_sigma_store(sigma):
    """Persist sigma value across tabs"""
    sigma = sigma if sigma else SIGMA_LEVEL
    return sigma, f"Current sigma level: {sigma}σ"


@app.callback(
    Output('sigma-store', 'data', allow_duplicate=True),
    Input('sigma-level', 'value'),
    prevent_initial_call=True,
)
def update_sigma_from_plot_setting(sigma):
    """Mirror the Plot Setting sigma slider into the shared sigma-store so
    Product plots/statistics update when the user changes it there."""
    return sigma if sigma else SIGMA_LEVEL


@app.callback(
    Output('cache-clear-result', 'children'),
    Input('config-clear-cache-btn', 'n_clicks'),
    prevent_initial_call=True
)
def clear_cache_callback(n_clicks):
    """Clear the persistent data cache and show result message"""
    if n_clicks is None or n_clicks == 0:
        return html.Div()
    
    success = clear_dashboard_cache()
    
    if success:
        return dbc.Alert([
            html.Strong("✅ Cache Cleared Successfully!"),
            html.P("The dashboard will reload from source files on the next launch.", className="mb-0 mt-2")
        ], color="success")
    else:
        return dbc.Alert([
            html.Strong("⚠️ Error Clearing Cache"),
            html.P("Check the console for error details.", className="mb-0 mt-2")
        ], color="danger")


@app.callback(
    Output('config-refresh-result', 'children'),
    Input('config-refresh-btn', 'n_clicks'),
    prevent_initial_call=True
)
def refresh_config_callback(n_clicks):
    """Reload the configuration files from Config folder"""
    if n_clicks is None or n_clicks == 0:
        return html.Div()
    
    try:
        # Reload config files
        data_store.loaded = False
        if not data_store.load_data():
            raise RuntimeError(data_store.load_error or 'No data loaded')
        
        return dbc.Alert([
            html.Strong("✅ Configuration Reloaded Successfully!"),
            html.P(f"Loaded {len(data_store.config_data)} test configurations from Config folder.", 
                   className="mb-0 mt-2")
        ], color="success")
    except Exception as e:
        return dbc.Alert([
            html.Strong("⚠️ Error Reloading Configuration"),
            html.P(f"Details: {str(e)}", className="mb-0 mt-2 small")
        ], color="danger")


# Batch callback removed - all data processing integrated into main script


@app.callback(
    Output('product-details-content', 'children'),
    Input('product-selector', 'value'),
    Input('sigma-store', 'data'),
    Input('filter-store', 'data'),
    Input('product-plot-count', 'value'),
    Input('product-unit-filter', 'value'),
    Input('product-lot-filter', 'value'),
    Input('product-program-filter', 'value'),
    Input('product-test-filter', 'value'),
    Input('product-devrev-filter', 'value'),
    Input('product-bin-filter', 'value'),
    Input('product-thermal-filter', 'value'),
    Input('product-bom-filter', 'value'),
    Input('product-pkg-filter', 'value'),
)
def update_product_details(product, sigma_value, filter_data, plot_count,
                          unit_filter, lot_filter, program_filter, test_filter,
                          devrev_filter, bin_filter, thermal_filter, bom_filter=None, pkg_filter=None):
    """Update product details - show all tests individually with applied filters"""
    if not product or data_store.df.empty:
        return html.Div("Select a product")
    
    sigma = sigma_value if sigma_value else SIGMA_LEVEL
    filters = _product_filters(filter_data, product, unit_filter, lot_filter, program_filter,
                               test_filter, devrev_filter, bin_filter, thermal_filter, bom_filter, pkg_filter)
    counts = data_store.backend.overview(filters)
    if not counts['readings']:
        return dbc.Alert("No data matches the active filters.", color="warning")
    display_name = PRODUCT_DISPLAY_NAMES.get(product, product.replace('_', ' '))
    
    # Test summary
    tests = data_store.backend.values('Test', filters)
    product_limits = data_store.product_limits or {}
    
    stats = data_store.backend.test_stats(filters).reindex(tests)
    stats['ucl'] = stats['mean'] + sigma * stats['std']
    stats['lcl'] = stats['mean'] - sigma * stats['std']
    
    spec_limits = {t: find_spec_limits(product, t, product_limits) for t in tests}
    stats_rows = []
    for test in tests:
        if test not in stats.index or pd.isna(stats.at[test, 'count']) or stats.at[test, 'count'] == 0:
            continue
        s_ucl, s_lcl = spec_limits.get(test, (None, None))
        stats_rows.append({
            'Test': test,
            'Count': int(stats.at[test, 'count']),
            'Mean': round(float(stats.at[test, 'mean']), 4),
            'Std Dev': round(float(stats.at[test, 'std']), 4),
            f'UCL ({sigma}\u03c3)': round(float(stats.at[test, 'ucl']), 4),
            f'LCL ({sigma}\u03c3)': round(float(stats.at[test, 'lcl']), 4),
            'Spec UCL': '' if s_ucl is None else round(s_ucl, 4),
            'Spec LCL': '' if s_lcl is None else round(s_lcl, 4),
        })
    stats_df = pd.DataFrame(stats_rows)
    
    summary_table = html.Div([
        html.H5(f"\U0001f4ca Statistical Summary per Test (Sigma = {sigma}\u03c3)", className="mb-3 mt-2"),
        dbc.Table.from_dataframe(
            stats_df, striped=True, bordered=True, hover=True, size="sm",
            style={'fontSize': '0.85em'}
        ) if not stats_df.empty else html.P("No statistics available", className="text-muted")
    ], className="mb-4")
    
    # Create plots for all tests (limited by user selection on the Product page)
    try:
        n_show = int(plot_count) if plot_count is not None else 5
    except (TypeError, ValueError):
        n_show = 5
    plot_tests = tests if n_show == 0 else tests[:n_show]

    # Plotly defaults that disable expensive interactions per chart.
    light_config = {'displaylogo': False, 'responsive': True}

    test_plots = []
    for test in plot_tests:
        max_points_per_plot = max(1, min(5000, 200000 // max(1, len(plot_tests))))
        test_data = data_store.backend.plot_rows(dict(filters, Test=test), limit=max_points_per_plot)
        if test_data is None or test_data.empty:
            continue
        
        # Memory-efficient: Downsample if dataset is too large to avoid memory allocation errors
        data_size = int(stats.at[test, 'count'])
        
        if data_size > max_points_per_plot:
            # Downsample using random sampling (preserves distribution)
            test_data_plot = test_data.head(max_points_per_plot)
            sample_info = f" (showing {max_points_per_plot:,} of {data_size:,} points)"
        else:
            test_data_plot = test_data
            sample_info = ""
        
        # WebGL scatter renders 10–100x faster than SVG for many points.
        fig = go.Figure()
        
        # Convert to lists instead of large numpy arrays to avoid memory allocation errors
        x_data = test_data_plot['Measurement_Point'].tolist() if 'Measurement_Point' in test_data_plot.columns else test_data_plot.index.tolist()
        y_data = test_data_plot['Value'].tolist() if 'Value' in test_data_plot.columns else []
        # Ensure unit_ids are properly encoded
        unit_ids = [str(x).encode('utf-8', errors='ignore').decode('utf-8', errors='ignore') for x in test_data_plot['Unit_ID'].astype(str).tolist()] if 'Unit_ID' in test_data_plot.columns else [''] * len(test_data_plot)
        
        # Skip if no valid data
        if not y_data:
            continue
        
        fig.add_trace(go.Scattergl(
            x=x_data,
            y=y_data,
            mode='markers',
            name='Data',
            marker=dict(size=6, color='blue', opacity=0.6),
            customdata=unit_ids,
            hovertemplate='VISUAL_ID: %{customdata}<br>Test Result: %{y:.4f}<br>Sensor: %{x}<extra></extra>',
        ))
        
        # Reuse the stats we already computed above instead of recomputing.
        if test in stats.index and stats.at[test, 'count']:
            mean = float(stats.at[test, 'mean'])
            ucl = float(stats.at[test, 'ucl'])
            lcl = float(stats.at[test, 'lcl'])
            fig.add_hline(
                y=mean, line_dash="dash", line_color="green",
                annotation_text=f"Mean = {mean:.4f}",
                annotation_position="top left",
                annotation_font_color="green"
            )
            fig.add_hline(
                y=ucl, line_dash="dot", line_color="red",
                annotation_text=f"UCL ({sigma}\u03c3) = {ucl:.4f}",
                annotation_position="top left",
                annotation_font_color="red"
            )
            fig.add_hline(
                y=lcl, line_dash="dot", line_color="red",
                annotation_text=f"LCL ({sigma}\u03c3) = {lcl:.4f}",
                annotation_position="bottom left",
                annotation_font_color="red"
            )
        
        # Overlay configured spec (default) UCL / LCL when available
        spec_ucl, spec_lcl = spec_limits.get(test, (None, None))
        if spec_ucl is not None:
            fig.add_hline(
                y=spec_ucl, line_dash="dashdot", line_color="#ff8c00", line_width=2,
                annotation_text=f"Spec UCL = {spec_ucl:g}",
                annotation_position="top right",
                annotation_font_color="#ff8c00"
            )
        if spec_lcl is not None:
            fig.add_hline(
                y=spec_lcl, line_dash="dashdot", line_color="#ff8c00", line_width=2,
                annotation_text=f"Spec LCL = {spec_lcl:g}",
                annotation_position="bottom right",
                annotation_font_color="#ff8c00"
            )
        
        fig.update_layout(
            title=str(test).encode('utf-8', errors='ignore').decode('utf-8', errors='ignore') + sample_info,
            xaxis_title='Sensor',
            yaxis_title='Test Result',
            showlegend=False, 
            margin=dict(t=40, b=40),
            height=400
        )
        
        test_plots.append(html.Div([
            dcc.Graph(figure=fig, config=light_config),
            html.Hr()
        ]))
    
    return html.Div([
        html.H4(f"\U0001f4ca {display_name}", className="mb-3"),
        
        dbc.Row([
            dbc.Col([
                html.Strong("Tests:"), f" {len(tests)}"
            ], width=3),
            dbc.Col([
                html.Strong("Units:"), f" {counts['units']}"
            ], width=3),
            dbc.Col([
                html.Strong("Lots:"), f" {counts['lots']}"
            ], width=3),
            dbc.Col([
                html.Strong("Readings:"), f" {counts['readings']:,}"
            ], width=3),
        ], className="mb-4"),
        
        html.Hr(),
        
        summary_table,
        
        html.Hr(),
        
        html.H5(f"\U0001f4c8 Individual Test Plots (Showing {len(plot_tests)} of {len(tests)} tests):", className="mb-3"),
        html.Div(test_plots),
        
        html.P("Scroll down to see all test plots", className="text-muted") if len(plot_tests) < len(tests) else html.Div(),
    ])


# Store for caching plot figures during export
_cached_product_plots = {'product': None, 'plots': []}


@app.callback(
    Output('product-export-status', 'children'),
    Input('product-export-pdf-btn', 'n_clicks'),
    State('product-selector', 'value'),
    State('product-plot-count', 'value'),
    State('filter-store', 'data'),
    State('product-unit-filter', 'value'),
    State('product-lot-filter', 'value'),
    State('product-program-filter', 'value'),
    State('product-test-filter', 'value'),
    State('product-devrev-filter', 'value'),
    State('product-bin-filter', 'value'),
    State('product-thermal-filter', 'value'),
    State('product-bom-filter', 'value'),
    State('product-pkg-filter', 'value'),
    prevent_initial_call=True
)
def export_plots_to_pdf(n_clicks, product, plot_count, filter_data, unit_filter, lot_filter, 
                        program_filter, test_filter, devrev_filter, bin_filter, thermal_filter, bom_filter=None, pkg_filter=None):
    """Export all product plots to PDF file"""
    if not product or data_store.df.empty:
        return dbc.Alert("No product selected or no data available", color="warning", dismissable=True)
    
    try:
        filters = _product_filters(filter_data, product, unit_filter, lot_filter, program_filter,
                       test_filter, devrev_filter, bin_filter, thermal_filter, bom_filter, pkg_filter)
        df = data_store.backend.plot_rows(filters, limit=1)
        if df is None or df.empty:
            return dbc.Alert("No data matches the active filters", color="warning", dismissable=True)
        
        product_df = df[df['Product_Name'].isin([product])]
        if product_df.empty or 'Value' not in product_df.columns:
            return dbc.Alert("No data for selected product", color="warning", dismissable=True)
        
        # Get tests for this product
        tests = data_store.backend.values('Test', filters)
        if not tests:
            return dbc.Alert("No tests found for this product", color="warning", dismissable=True)
        
        # Limit plots if needed
        if plot_count and plot_count > 0:
            tests = tests[:plot_count]
        
        # Create output filename
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        pdf_filename = f"{OUTPUT_FOLDER}/Product_{product}_{timestamp}.pdf"
        os.makedirs(OUTPUT_FOLDER, exist_ok=True)
        
        # Create PDF
        doc = SimpleDocTemplate(pdf_filename, pagesize=letter, topMargin=0.5*inch, bottomMargin=0.5*inch)
        story = []
        styles = getSampleStyleSheet()
        
        # Add title
        title_style = ParagraphStyle(
            'CustomTitle',
            parent=styles['Heading1'],
            fontSize=18,
            textColor='#0066cc',
            spaceAfter=12,
        )
        story.append(Paragraph(f"Product Analysis: {product}", title_style))
        story.append(Paragraph(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", styles['Normal']))
        story.append(Spacer(1, 0.3*inch))
        
        # Generate and add plots
        plot_count_actual = 0
        for test in tests:
            try:
                test_df = data_store.backend.plot_rows(dict(filters, Test=test))
                if test_df.empty or 'Value' not in test_df.columns:
                    continue
                
                # Create scatter plot
                fig = px.scatter(
                    test_df,
                    x='Measurement_Point' if 'Measurement_Point' in test_df.columns else test_df.index,
                    y='Value',
                    title=f"Test: {test} (up to 5,000 sampled readings)",
                    labels={'Value': 'Reading Value'},
                    height=400
                )
                
                # Convert plotly figure to image
                img_bytes = pio.to_image(fig, format="png", width=1000, height=400)
                img_file = io.BytesIO(img_bytes)
                
                # Add to PDF
                if plot_count_actual > 0:
                    story.append(PageBreak())
                
                story.append(Paragraph(f"<b>{test}</b>", styles['Heading2']))
                story.append(Spacer(1, 0.1*inch))
                
                img = RLImage(img_file, width=6.5*inch, height=2.6*inch)
                story.append(img)
                story.append(Spacer(1, 0.3*inch))
                
                plot_count_actual += 1
                
            except Exception as e:
                print(f"Warning: Could not generate plot for {test}: {e}")
                continue
        
        if plot_count_actual == 0:
            return dbc.Alert("No plots could be generated", color="danger", dismissable=True)
        
        # Generate PDF
        doc.build(story)
        
        return dbc.Alert([
            html.H5("✅ PDF Export Successful!", className="alert-heading"),
            html.P(f"📄 File: {pdf_filename}"),
            html.P(f"📊 Plots included: {plot_count_actual}"),
            html.P("The PDF has been saved to the HTML_Output folder.", className="mb-0")
        ], color="success", dismissable=True)
        
    except Exception as e:
        return dbc.Alert([
            html.H5("❌ Error exporting PDF", className="alert-heading"),
            html.P(f"Error: {str(e)}")
        ], color="danger", dismissable=True)


@app.callback(
    Output('product-export-status', 'children', allow_duplicate=True),
    Input('product-export-images-btn', 'n_clicks'),
    State('product-selector', 'value'),
    State('product-plot-count', 'value'),
    State('filter-store', 'data'),
    State('product-unit-filter', 'value'),
    State('product-lot-filter', 'value'),
    State('product-program-filter', 'value'),
    State('product-test-filter', 'value'),
    State('product-devrev-filter', 'value'),
    State('product-bin-filter', 'value'),
    State('product-thermal-filter', 'value'),
    State('product-bom-filter', 'value'),
    State('product-pkg-filter', 'value'),
    prevent_initial_call=True
)
def export_plots_as_images(n_clicks, product, plot_count, filter_data, unit_filter, lot_filter,
                          program_filter, test_filter, devrev_filter, bin_filter, thermal_filter, bom_filter=None, pkg_filter=None):
    """Export all product plots as individual PNG images"""
    if not product or data_store.df.empty:
        return dbc.Alert("No product selected or no data available", color="warning", dismissable=True)
    
    try:
        filters = _product_filters(filter_data, product, unit_filter, lot_filter, program_filter,
                       test_filter, devrev_filter, bin_filter, thermal_filter, bom_filter, pkg_filter)
        df = data_store.backend.plot_rows(filters, limit=1)
        if df is None or df.empty:
            return dbc.Alert("No data matches the active filters", color="warning", dismissable=True)
        
        product_df = df[df['Product_Name'].isin([product])]
        if product_df.empty or 'Value' not in product_df.columns:
            return dbc.Alert("No data for selected product", color="warning", dismissable=True)
        
        # Get tests
        tests = data_store.backend.values('Test', filters)
        if not tests:
            return dbc.Alert("No tests found for this product", color="warning", dismissable=True)
        
        # Limit plots if needed
        if plot_count and plot_count > 0:
            tests = tests[:plot_count]
        
        # Create output folder
        export_folder = f"{OUTPUT_FOLDER}/Product_{product}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        os.makedirs(export_folder, exist_ok=True)
        
        # Generate images
        plot_count_actual = 0
        for test in tests:
            try:
                test_df = data_store.backend.plot_rows(dict(filters, Test=test))
                if test_df.empty or 'Value' not in test_df.columns:
                    continue
                
                # Create plot
                fig = px.scatter(
                    test_df,
                    x='Measurement_Point' if 'Measurement_Point' in test_df.columns else test_df.index,
                    y='Value',
                    title=f"Test: {test} (up to 5,000 sampled readings)",
                    labels={'Value': 'Reading Value'},
                    height=600,
                    width=1200
                )
                
                # Save as PNG
                safe_test_name = "".join(c for c in test if c.isalnum() or c in ('-', '_')).rstrip()
                img_filename = f"{export_folder}/{plot_count_actual+1:03d}_{safe_test_name}.png"
                fig.write_image(img_filename)
                
                plot_count_actual += 1
                
            except Exception as e:
                print(f"Warning: Could not save image for {test}: {e}")
                continue
        
        if plot_count_actual == 0:
            return dbc.Alert("No images could be generated", color="danger", dismissable=True)
        
        return dbc.Alert([
            html.H5("✅ Image Export Successful!", className="alert-heading"),
            html.P(f"📁 Folder: {export_folder}"),
            html.P(f"🖼️  Images saved: {plot_count_actual}"),
            html.P("Images have been saved to the HTML_Output folder.", className="mb-0")
        ], color="success", dismissable=True)
        
    except Exception as e:
        return dbc.Alert([
            html.H5("❌ Error exporting images", className="alert-heading"),
            html.P(f"Error: {str(e)}")
        ], color="danger", dismissable=True)


@app.callback(
    Output('bin-fbin-selector', 'options'),
    Output('bin-fbin-selector', 'value'),
    Input('bin-product-selector', 'value'),
    Input('ta-lot-filter', 'value'),
    Input('ta-program-filter', 'value'),
    Input('ta-visual-filter', 'value'),
    Input('ta-test-filter', 'value'),
)
def update_bin_fbin_options(product, lots, programs, units, tests):
    """Populate Functional Bin dropdown based on product filter + advanced filters"""
    if data_store.df.empty or 'Functional_Bin' not in data_store.df.columns:
        return [], None
    filters = _query_filters(Product_Name=product, Lot=lots, Program_Name=programs, Unit_ID=units, Test=tests)
    bins = sorted([value for value in data_store.backend.values('Functional_Bin', filters) if value],
                  key=lambda value: (len(value), value))
    options = [{'label': f"Bin {b}", 'value': b} for b in bins]
    return options, bins[0] if bins else None


@app.callback(
    Output('bin-commonality-output', 'children'),
    Input('bin-product-selector', 'value'),
    Input('bin-fbin-selector', 'value'),
    Input('bin-param-selector', 'value'),
    Input('filter-store', 'data'),
    Input('ta-lot-filter', 'value'),
    Input('ta-program-filter', 'value'),
    Input('ta-visual-filter', 'value'),
    Input('ta-test-filter', 'value'),
)
def update_bin_commonality(product, fbin, param, filter_data, lots, programs, units, tests):
    """Show commonality breakdown of selected Functional Bin against chosen parameter"""
    if data_store.df.empty or not fbin or not param:
        return html.Div("Select Functional Bin and parameter.", className="text-muted")
    
    filters = _query_filters(filter_data, Product_Name=product, Lot=lots, Program_Name=programs,
                             Unit_ID=units, Test=tests)
    counts = data_store.backend.commonality(filters, fbin, param)
    if counts.empty:
        return dbc.Alert(f"No units found with Functional Bin = {fbin}.", color="info")
    total = int(counts.pop('total').iloc[0])
    counts.columns = [param.replace('_', ' '), 'Failed Units']
    counts['% of Bin'] = (counts['Failed Units'] / total * 100).round(2)
    
    # Bar chart
    fig = go.Figure(go.Bar(
        x=counts[param.replace('_', ' ')].astype(str),
        y=counts['Failed Units'],
        text=counts['Failed Units'],
        textposition='outside',
        marker_color='#dc3545',
        hovertemplate='<b>%{x}</b><br>Units: %{y}<extra></extra>',
    ))
    fig.update_layout(
        title=f"Bin {fbin} Commonality by {param.replace('_', ' ')} (Total units: {total})",
        xaxis_title=param.replace('_', ' '),
        yaxis_title="Failed Unit Count",
        height=450, xaxis=dict(tickangle=-30)
    )
    
    return html.Div([
        dcc.Graph(figure=fig),
        html.H5(f"Bin {fbin} commonality table", className="mt-3"),
        dbc.Table.from_dataframe(counts, striped=True, bordered=True, hover=True, size="sm"),
    ])


@app.callback(
    Output('repeat-bin-selector', 'options'),
    Input('repeat-product-selector', 'value'),
    Input('ta-lot-filter', 'value'),
    Input('ta-program-filter', 'value'),
    Input('ta-visual-filter', 'value'),
    Input('ta-test-filter', 'value'),
)
def update_repeat_bin_options(product, lots, programs, units, tests):
    if data_store.df.empty or 'Functional_Bin' not in data_store.df.columns:
        return []
    return update_bin_fbin_options(product, lots, programs, units, tests)[0]


@app.callback(
    Output('repeat-failure-output', 'children'),
    Input('repeat-product-selector', 'value'),
    Input('repeat-bin-selector', 'value'),
    Input('repeat-min-count', 'value'),
    Input('filter-store', 'data'),
    Input('ta-lot-filter', 'value'),
    Input('ta-program-filter', 'value'),
    Input('ta-visual-filter', 'value'),
    Input('ta-test-filter', 'value'),
)
def update_repeat_failures(product, fbin, min_count, filter_data, lots, programs, units, tests):
    """Identify units (VISUAL_ID) that failed repeatedly across tests"""
    if data_store.df.empty or 'Functional_Bin' not in data_store.df.columns:
        return html.Div("No data available.", className="text-muted")
    
    min_c = int(min_count) if min_count else 2
    filters = _query_filters(filter_data, Product_Name=product, Lot=lots, Program_Name=programs,
                             Unit_ID=units, Test=tests, Functional_Bin=fbin or None)
    repeat_df, total_units, repeat_units = data_store.backend.repeat_failures(filters, min_c)
    
    summary_cards = dbc.Row([
        dbc.Col(dbc.Card(dbc.CardBody([
            html.H4(f"{total_units:,}", className="text-danger mb-0"),
            html.P("Total Failing Units", className="text-muted mb-0")
        ]), className="text-center"), md=4),
        dbc.Col(dbc.Card(dbc.CardBody([
            html.H4(f"{repeat_units:,}", className="text-warning mb-0"),
            html.P(f"Units With ≥{min_c} Failures", className="text-muted mb-0")
        ]), className="text-center"), md=4),
        dbc.Col(dbc.Card(dbc.CardBody([
            html.H4(f"{(repeat_units / total_units * 100) if total_units else 0:.1f}%", className="text-info mb-0"),
            html.P("Repeat Rate", className="text-muted mb-0")
        ]), className="text-center"), md=4),
    ], className="mb-3 mt-2")
    
    if repeat_df.empty:
        return html.Div([summary_cards, dbc.Alert("No repeat-failure units at this threshold.", color="success")])
    
    table = dbc.Table.from_dataframe(
        repeat_df.head(200).rename(columns={'Unit_ID': 'VISUAL_ID'}),
        striped=True, bordered=True, hover=True, size="sm",
        style={'fontSize': '0.85em'}
    )
    
    return html.Div([
        summary_cards,
        html.H5(f"Repeat-failure units (top {len(repeat_df)} of {repeat_units})", className="mt-2"),
        table,
    ])


def _scan_program_directories():
    """Scan UNC network shares for program folders and module files"""
    found = {}  # program_folder_name -> { module: [file_paths] }
    errors = []
    for pattern in PROGRAM_DIRS:
        try:
            matches = glob.glob(pattern)
        except Exception as e:
            errors.append(f"{pattern}: {e}")
            continue
        for folder in matches:
            if not os.path.isdir(folder):
                continue
            folder_name = os.path.basename(folder.rstrip('\\/'))
            modules_found = {}
            try:
                for root, _dirs, files in os.walk(folder):
                    for fname in files:
                        upper = fname.upper()
                        for module in PROGRAM_MODULES:
                            if module in upper:
                                modules_found.setdefault(module, []).append(os.path.join(root, fname))
            except Exception as e:
                errors.append(f"{folder}: {e}")
                continue
            if modules_found:
                found[folder_name] = modules_found
    return found, errors


# Cache of last scan
_program_scan_cache = {'data': None, 'errors': None}


@app.callback(
    Output('prog-name-selector', 'options'),
    Output('prog-name-selector', 'value'),
    Input('prog-module-selector', 'value'),
    Input('prog-rescan-btn', 'n_clicks')
)
def update_program_options(module, n_clicks):
    """Populate Program dropdown from scanned UNC shares"""
    ctx = callback_context
    rescan = bool(ctx.triggered and 'prog-rescan-btn' in ctx.triggered[0]['prop_id'])
    if rescan or _program_scan_cache['data'] is None:
        data, errors = _scan_program_directories()
        _program_scan_cache['data'] = data
        _program_scan_cache['errors'] = errors
    data = _program_scan_cache['data'] or {}
    programs = sorted([name for name, mods in data.items() if module in mods])
    options = [{'label': p, 'value': p} for p in programs]
    return options, programs[0] if programs else None


@app.callback(
    Output('prog-config-output', 'children'),
    Input('prog-module-selector', 'value'),
    Input('prog-name-selector', 'value')
)
def update_program_config(module, program):
    """Display module configuration files for the selected Test Program"""
    data = _program_scan_cache.get('data')
    errors = _program_scan_cache.get('errors') or []
    
    error_box = None
    if errors:
        error_box = dbc.Alert(
            [html.Strong("Scan warnings:"), html.Br(),
             *[html.Span([e, html.Br()]) for e in errors[:5]]],
            color="warning", className="small"
        )
    
    if not data:
        return html.Div([
            error_box or html.Div(),
            dbc.Alert(
                "No programs found. Verify network access to the UNC paths above, then click Rescan.",
                color="info"
            )
        ])
    
    if not program or program not in data:
        return html.Div([
            error_box or html.Div(),
            dbc.Alert("Select a Program from the dropdown to view its configuration.", color="info")
        ])
    
    files = data[program].get(module, [])
    if not files:
        return html.Div([
            error_box or html.Div(),
            dbc.Alert(f"No '{module}' files found in {program}.", color="info")
        ])
    
    rows = []
    for fpath in files[:200]:
        try:
            size_kb = os.path.getsize(fpath) / 1024.0
        except Exception:
            size_kb = 0
        rows.append({
            'File': os.path.basename(fpath),
            'Relative Path': os.path.relpath(fpath, start=os.path.dirname(os.path.dirname(fpath)) or fpath),
            'Size (KB)': f"{size_kb:.1f}",
            'Full Path': fpath,
        })
    table_df = pd.DataFrame(rows)
    
    return html.Div([
        error_box or html.Div(),
        html.H5(f"📁 {program} → {module}", className="mt-2"),
        html.P(f"Found {len(files)} matching file(s).", className="text-muted"),
        dbc.Table.from_dataframe(
            table_df, striped=True, bordered=True, hover=True, size="sm",
            style={'fontSize': '0.8em'}
        ),
    ])

@app.callback(
    Output('refresh-status', 'children'),
    Input('refresh-data-btn', 'n_clicks'),
    prevent_initial_call=True
)
def refresh_data(n_clicks):
    """Refresh data from files"""
    data_store.loaded = False
    success = data_store.load_data()
    
    if success:
        return dbc.Alert(
            f"✅ Data refreshed successfully! Loaded {data_store.overview['readings']:,} readings.",
            color="success",
            dismissable=True
        )
    else:
        return dbc.Alert(
            "❌ Failed to load data. Check that files exist in Input_data folder.",
            color="danger",
            dismissable=True
        )


@app.callback(
    Output('refresh-status', 'children', allow_duplicate=True),
    Output('filter-store', 'data', allow_duplicate=True),
    Input('clear-cache-btn', 'n_clicks'),
    prevent_initial_call=True,
)
def clear_cache_and_reset(n_clicks):
    """Delete parquet cache files and reset the in-memory data store so the next
    page load re-reads everything from scratch."""
    removed = 0
    failed = []
    if not clear_dashboard_cache():
        return dbc.Alert('Could not invalidate the data cache.', color='danger'), dash.no_update
    data_store.loaded = False
    data_store.df = pd.DataFrame()
    data_store.config_data = None
    data_store.product_limits = None
    _program_scan_cache['data'] = None
    _program_scan_cache['errors'] = None
    
    msg = [
        html.Strong("Data cache invalidated. Source files were not changed."),
        html.Br(),
        "Click 'Refresh Data' (or open any page) to reload from source.",
    ]
    if failed:
        msg += [html.Br(), html.Small(f"Could not delete: {'; '.join(failed[:3])}", className="text-muted")]
    return dbc.Alert(msg, color="warning", dismissable=True), {}

@app.callback(
    Output('profiles-table-container', 'children'),
    Output('profile-form-status', 'children'),
    Input('profile-create-btn', 'n_clicks'),
    State('profile-work-week', 'value'),
    State('profile-product', 'value'),
    State('profile-test-program', 'value'),
    State('profile-lots', 'value'),
    State('profile-notes', 'value'),
    prevent_initial_call=True,
)
def create_profile_callback(n_clicks, work_week, product, test_program, lots, notes):
    """Save a new (or updated) work-week profile, then attempt to pull and crunch its data."""
    active_slug = data_store.active_profile.get('slug') if data_store.active_profile else None
    try:
        profile = profile_store.save_profile(work_week, product, test_program, lots, notes)
    except profile_store.ProfileError as e:
        return _render_profiles_table(active_slug), dbc.Alert(str(e), color="danger", dismissable=True)

    status = _pull_and_crunch_profile(profile)
    return _render_profiles_table(active_slug), status

@app.callback(
    Output('profiles-table-container', 'children', allow_duplicate=True),
    Output('profile-action-status', 'children'),
    Output('active-profile-store', 'data'),
    Input({'type': 'profile-open', 'index': ALL}, 'n_clicks'),
    Input({'type': 'profile-refresh', 'index': ALL}, 'n_clicks'),
    Input({'type': 'profile-delete', 'index': ALL}, 'n_clicks'),
    prevent_initial_call=True,
)
def profile_row_actions(open_clicks, refresh_clicks, delete_clicks):
    """Handle Open/Pull-Refresh/Delete clicks from the profiles table (pattern-matched per row)."""
    triggered = callback_context.triggered_id
    if not triggered or not (any(open_clicks) or any(refresh_clicks) or any(delete_clicks)):
        return dash.no_update, dash.no_update, dash.no_update

    action, slug = triggered['type'], triggered['index']

    if action == 'profile-delete':
        profile_store.delete_profile(slug)
        if data_store.active_profile and data_store.active_profile.get('slug') == slug:
            data_store.switch_to_main()
        active_slug = data_store.active_profile.get('slug') if data_store.active_profile else None
        return (_render_profiles_table(active_slug),
                dbc.Alert(f"Profile '{slug}' deleted.", color="warning", dismissable=True),
                active_slug)

    if action == 'profile-open':
        try:
            data_store.switch_to_profile(slug)
            status = dbc.Alert(f"Switched to profile '{slug}'. {data_store.load_status}",
                                color="success", dismissable=True)
        except Exception as e:
            status = dbc.Alert(f"Could not open profile '{slug}': {e}", color="danger", dismissable=True)
        active_slug = data_store.active_profile.get('slug') if data_store.active_profile else None
        return _render_profiles_table(active_slug), status, active_slug

    if action == 'profile-refresh':
        profile = profile_store.load_profile(slug)
        status = _pull_and_crunch_profile(profile)
        active_slug = data_store.active_profile.get('slug') if data_store.active_profile else None
        return _render_profiles_table(active_slug), status, active_slug

    return dash.no_update, dash.no_update, dash.no_update

@app.callback(
    Output('active-profile-store', 'data', allow_duplicate=True),
    Output('profile-action-status', 'children', allow_duplicate=True),
    Output('profiles-table-container', 'children', allow_duplicate=True),
    Input('profile-switch-main-btn', 'n_clicks'),
    prevent_initial_call=True,
)
def switch_to_main_callback(n_clicks):
    """Return the whole dashboard to the shared main dataset in input/."""
    data_store.switch_to_main()
    return (None, dbc.Alert("Switched back to the main dataset.", color="info", dismissable=True),
            _render_profiles_table(None))

@app.callback(
    Output('active-profile-banner', 'children'),
    Input('active-profile-store', 'data'),
)
def update_active_profile_banner(_data):
    """Small floating badge showing which dataset (main or profile) is currently active."""
    if data_store.active_profile:
        label = f"Active profile: {data_store.active_profile.get('work_week')}"
        if data_store.active_profile.get('product'):
            label += f" · {PRODUCT_DISPLAY_NAMES.get(data_store.active_profile['product'], data_store.active_profile['product'])}"
        return html.Div(label, className="text-white small px-3 py-1",
                         style={'backgroundColor': COLORS['sidebar_active']})
    return html.Div("Active dataset: Main (input/)", className="text-muted small px-3 py-1",
                     style={'backgroundColor': COLORS['light_bg']})

# ============================================================================
# RUN APPLICATION
# ============================================================================

def _launch_edge(url: str, delay: float = 2.0):
    """Open the dashboard URL in Microsoft Edge after the server is up."""
    import threading, subprocess, webbrowser, shutil

    def _open():
        edge_paths = [
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        ]
        exe = next((p for p in edge_paths if os.path.exists(p)), None) or shutil.which("msedge")
        try:
            if exe:
                subprocess.Popen([exe, url])
                print(f"\n🌐 Opened dashboard in Microsoft Edge: {url}")
            else:
                webbrowser.open(url)
                print(f"\n🌐 Edge not found; opened default browser: {url}")
        except Exception as e:
            print(f"[launcher] could not open browser: {e}")

    threading.Timer(delay, _open).start()


if __name__ == '__main__':
    sys.modules['atlas_thermal_dashboard_optimized'] = sys.modules[__name__]
    from run_dashboard import main

    main()
