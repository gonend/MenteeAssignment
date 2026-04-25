import yaml
import logging
from pathlib import Path
from typing import Dict, Any

# Set up a logger for the config module
logger = logging.getLogger(__name__)

class ConfigurationError(Exception):
    """Custom exception for critical configuration loading failures."""
    pass

def load_yaml_config(file_path: str | Path) -> Dict[str, Any]:
    """
    Safely loads and parses a YAML configuration file.
    
    Args:
        file_path: Path to the .yaml file (can be a string or Path object).
        
    Returns:
        A dictionary containing the parsed YAML data.
        
    Raises:
        ConfigurationError: If the file is missing, unreadable, or contains invalid YAML.
    """
    path = Path(file_path)
    
    # 1. Error Handling: Missing File
    if not path.exists():
        error_msg = f"CRITICAL: Configuration file not found at {path.absolute()}"
        logger.error(error_msg)
        raise ConfigurationError(error_msg)
        
    try:
        with open(path, 'r', encoding='utf-8') as f:
            # 2. Security & Parsing: Always use safe_load to prevent code execution
            config = yaml.safe_load(f)
            
        if config is None:
            logger.warning(f"Configuration file {path.name} is empty. Returning empty dictionary.")
            return {}
            
        return config
        
    # 3. Error Handling: Malformed YAML
    except yaml.YAMLError as e:
        error_msg = f"CRITICAL: Failed to parse YAML file {path.name}. Syntax error: {e}"
        logger.error(error_msg)
        raise ConfigurationError(error_msg)
        
    # Catch-all for unexpected I/O issues (e.g., permissions)
    except Exception as e:
        error_msg = f"CRITICAL: Unexpected error reading {path.name}: {e}"
        logger.error(error_msg)
        raise ConfigurationError(error_msg)