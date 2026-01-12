# Run this script on an iOS device. Known to work with a-Shell.
# Outputs a zip file containing the files that generate.py would scan.
# Transfer the zip to another device and unzip.
# Then run generate.py with BASE_PATH set to <unzip_path>/System/Library/.
# This avoids having to code on a computer, transfer generate.py to an iPhone,
# run generate.py on the phone, transfer the results back...
# Note: if the locations of preference manifests change,
# then this script needs to be updated too.

from zipfile import ZipFile
from plistlib import load
from pathlib import Path


BASE_PATH = Path("/System/Library")
# Folders in /System/Library/ known to contain bundles with Settings URLs
BUNDLE_LOCATIONS = (
	"BridgeManifests",
	"NanoPreferenceBundles",
	"PreferenceBundles",
	"PreferenceManifests",
	"PreferenceManifestsInternal"
)


with open(BASE_PATH / "CoreServices" / "SystemVersion.plist", "rb") as fp:
	ios_version: str = load(fp)["ProductVersion"]


def load_bundle(bundle_path: Path, zf: ZipFile):
	"""
	Load a bundle and add its URLs to the tree.
	:param bundle_path: Path to the bundle
	"""
	for subitem in bundle_path.glob("SettingsSearchManifest*.plist"):
		zf.write(str(subitem))
	for subitem in bundle_path.glob("SettingsSearchManifest*.loctable"):
		zf.write(str(subitem))
	for subitem in bundle_path.glob("SettingsSearchManifest*.strings"):
		zf.write(str(subitem))
	for subitem in bundle_path.glob("*.lproj/SettingsSearchManifest*.strings"):
		zf.write(str(subitem))


def scan_folder(folder_path: Path, zf: ZipFile):
	"""
	Scan a folder recursively for Settings URLs and load bundles if found.
	:param folder_path: Path to the folder to scan
	"""
	for file in folder_path.iterdir():
		if file.is_dir():
			if file.name.endswith(".bundle"):
				load_bundle(file, zf)
			elif file.name != "_CodeSignature":
				scan_folder(file, zf)


with ZipFile(f"iOS{ios_version}.zip", "x") as zf:
	# So that we have the system version specifier still
	zf.write(BASE_PATH / "CoreServices" / "SystemVersion.plist")
	# Load all bundles at known locations
	for bundle_location in BUNDLE_LOCATIONS:
		scan_folder(BASE_PATH / bundle_location, zf)
	# One known special case that isn't in a normal bundle
	load_bundle(BASE_PATH / "PrivateFrameworks" / "PBBridgeSupport.framework", zf)