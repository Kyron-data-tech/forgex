import os
import re
import json
import shutil
import unicodedata
from pathlib import Path
from typing import Optional, Tuple
from datetime import datetime, timezone
from PIL import Image

class StorageSecurityError(Exception):
    """Raised when a path traversal, symlink, or unauthorized access attempt is detected."""
    pass

class ImageValidationError(Exception):
    """Raised when an image fails format, size, or dimensional security checks."""
    pass

class StorageService:
    ALLOWED_IMAGE_FORMATS = {"JPEG", "JPG", "PNG", "WEBP", "GIF"}
    MAX_DIMENSION = 8000  # Max 8000x8000 pixels to prevent decompression bombs

    def __init__(self, root_dir: Optional[Path] = None, max_image_size: int = 10 * 1024 * 1024, min_free_disk: int = 2 * 1024 * 1024 * 1024):
        self.root_dir = Path(root_dir).resolve() if root_dir else Path.cwd().resolve()
        self.max_image_size = max_image_size
        self.min_free_disk = min_free_disk

    def set_root_dir(self, directory: Path) -> Path:
        resolved = Path(directory).resolve()
        if not resolved.exists():
            resolved.mkdir(parents=True, exist_ok=True)
        self.check_disk_space(resolved)
        self.root_dir = resolved
        return self.root_dir

    def check_disk_space(self, target_path: Optional[Path] = None) -> dict:
        """Verifies target directory has at least min_free_disk bytes free."""
        check_dir = Path(target_path or self.root_dir).resolve()
        while not check_dir.exists() and check_dir.parent != check_dir:
            check_dir = check_dir.parent
        usage = shutil.disk_usage(check_dir)
        is_sufficient = usage.free >= self.min_free_disk
        return {
            "total_bytes": usage.total,
            "used_bytes": usage.used,
            "free_bytes": usage.free,
            "free_gb": round(usage.free / (1024 ** 3), 2),
            "sufficient": is_sufficient,
            "min_required_gb": round(self.min_free_disk / (1024 ** 3), 2)
        }

    @staticmethod
    def sanitize_slug(text: str) -> str:
        """Converts arbitrary strings to safe alphanumeric hyphenated slugs."""
        if not text:
            return "unnamed"
        text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
        text = re.sub(r"[^\w\s-]", "", text.lower()).strip()
        slug = re.sub(r"[-\s]+", "-", text)
        return slug or "unnamed"

    @staticmethod
    def sanitize_filename(filename: str) -> str:
        """Sanitizes filename, stripping directory traversal characters and unsafe symbols."""
        filename = os.path.basename(filename.strip().replace("\\", "/"))
        stem, ext = os.path.splitext(filename)
        # Collapse multiple underscores and strip trailing punctuation from stem
        clean_stem = re.sub(r"_+", "_", re.sub(r"[^\w\-.]", "_", stem)).strip("._")
        clean_ext = re.sub(r"[^\w]", "", ext.lower())
        if not clean_stem:
            clean_stem = "file"
        if clean_ext:
            return f"{clean_stem}.{clean_ext}"
        return clean_stem

    def resolve_safe_path(self, relative_path: str, base_dir: Optional[Path] = None) -> Path:
        """
        Resolves path and guarantees it resides strictly inside base_dir or self.root_dir.
        Blocks path traversal (../), absolute escapes, and symbolic link trickery.
        """
        base = Path(base_dir or self.root_dir).resolve()
        clean_rel = relative_path.lstrip("/\\")
        candidate = (base / clean_rel).resolve()

        # Symlink check
        if os.path.islink(candidate):
            raise StorageSecurityError(f"Symbolic link traversal prohibited: {candidate}")

        # Parent containment check
        try:
            candidate.relative_to(base)
        except ValueError:
            raise StorageSecurityError(f"Path traversal attempted outside base directory: {candidate}")

        return candidate

    def validate_image_bytes(self, data: bytes) -> Tuple[str, int, int]:
        """
        Validates raw image data: size limit, authentic image format, dimensions.
        Returns: (format, width, height)
        """
        if len(data) > self.max_image_size:
            raise ImageValidationError(f"Image exceeds maximum size limit of {self.max_image_size} bytes")
        
        if len(data) < 16:
            raise ImageValidationError("Invalid file: too small to be a genuine image")

        try:
            import io
            with Image.open(io.BytesIO(data)) as img:
                img_format = img.format.upper() if img.format else ""
                if img_format not in self.ALLOWED_IMAGE_FORMATS:
                    raise ImageValidationError(f"Unsupported image format: {img_format}. Allowed: {self.ALLOWED_IMAGE_FORMATS}")
                
                width, height = img.size
                if width <= 0 or height <= 0:
                    raise ImageValidationError("Invalid image dimensions (zero or negative)")
                if width > self.MAX_DIMENSION or height > self.MAX_DIMENSION:
                    raise ImageValidationError(f"Image dimensions ({width}x{height}) exceed maximum allowed {self.MAX_DIMENSION}px")

                img.verify()
                return img_format, width, height
        except Exception as e:
            if isinstance(e, ImageValidationError):
                raise
            raise ImageValidationError(f"Image verification failed: {str(e)}")

    def optimize_image_bytes(
        self,
        image_data: bytes,
        max_dim: int = 1200,
        quality: int = 90,
        target_format: Optional[str] = None
    ) -> Tuple[bytes, str, int, int]:
        """
        Normalizes, resizes, and optimizes image data:
        - Downscales proportionally if width or height > max_dim
        - Converts RGBA/P/CMYK appropriately
        - Re-encodes cleanly in target format (default JPEG, or PNG)
        - Returns: (optimized_bytes, format_str, width, height)
        """
        import io
        with Image.open(io.BytesIO(image_data)) as img:
            try:
                from PIL import ImageOps
                img = ImageOps.exif_transpose(img)
            except Exception:
                pass

            save_fmt = (target_format or img.format or "JPEG").upper()
            if save_fmt not in ("JPEG", "PNG", "WEBP"):
                save_fmt = "JPEG"

            if save_fmt == "JPEG":
                if img.mode in ("RGBA", "LA"):
                    bg = Image.new("RGB", img.size, (255, 255, 255))
                    bg.paste(img, mask=img.split()[-1])
                    img = bg
                elif img.mode != "RGB":
                    img = img.convert("RGB")
            elif save_fmt == "PNG":
                if img.mode not in ("RGB", "RGBA"):
                    img = img.convert("RGBA" if "transparency" in img.info else "RGB")

            width, height = img.size
            if width > max_dim or height > max_dim:
                img.thumbnail((max_dim, max_dim), Image.Resampling.LANCZOS)
                width, height = img.size

            out_buf = io.BytesIO()
            if save_fmt == "JPEG":
                img.save(out_buf, format="JPEG", quality=quality, optimize=True)
            elif save_fmt == "PNG":
                img.save(out_buf, format="PNG", optimize=True)
            else:
                img.save(out_buf, format=save_fmt)

            return out_buf.getvalue(), save_fmt, width, height

    def save_image_with_metadata(
        self,
        person_slug: str,
        filename: str,
        image_data: bytes,
        metadata: dict,
        base_dir: Optional[Path] = None
    ) -> Tuple[Path, Path]:
        """
        Safely saves an image into <user_dir>/<person-slug>/<sanitized_filename>
        along with a sidecar <sanitized_filename>.meta.json file.
        Auto-normalizes dimensions and format for web publishing.
        Returns: (saved_image_path, sidecar_json_path)
        """
        safe_slug = self.sanitize_slug(person_slug)
        safe_filename = self.sanitize_filename(filename)

        # Validate original bytes
        self.validate_image_bytes(image_data)
        self.check_disk_space()

        # Determine target format from filename
        ext = Path(safe_filename).suffix.lower()
        target_fmt = "PNG" if ext == ".png" else "JPEG"

        # Optimize and size-correct image
        try:
            image_data, img_format, width, height = self.optimize_image_bytes(
                image_data, target_format=target_fmt
            )
        except Exception:
            img_format, width, height = self.validate_image_bytes(image_data)

        dest_folder = self.resolve_safe_path(safe_slug, base_dir=base_dir)
        dest_folder.mkdir(parents=True, exist_ok=True)

        image_path = self.resolve_safe_path(f"{safe_slug}/{safe_filename}", base_dir=base_dir)
        sidecar_path = self.resolve_safe_path(f"{safe_slug}/{safe_filename}.meta.json", base_dir=base_dir)

        with open(image_path, "wb") as f:
            f.write(image_data)

        meta_payload = {
            "original_filename": filename,
            "saved_filename": safe_filename,
            "person_slug": safe_slug,
            "format": img_format,
            "dimensions": {"width": width, "height": height},
            "size_bytes": len(image_data),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "source_url": metadata.get("source_url", ""),
            "license": metadata.get("license", "Unknown"),
            "author": metadata.get("author", "Unknown"),
            "attribution": metadata.get("attribution", ""),
            "page_url": metadata.get("page_url", "")
        }

        with open(sidecar_path, "w", encoding="utf-8") as f:
            json.dump(meta_payload, f, indent=2, ensure_ascii=False)

        return image_path, sidecar_path

    def delete_user_file(self, target_path: Path):
        raise PermissionError(
            "ForgeX Safety Policy: Physical deletion of user files is strictly prohibited. "
            "Archive the database record instead."
        )
