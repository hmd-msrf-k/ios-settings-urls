from os import makedirs
from shutil import copy
from pathlib import Path
from plistlib import load as load_plist
from urllib.parse import urlparse, parse_qs, quote
from json import dump as dump_json, load as load_json
from typing import Generator, Iterable, Self, TypeAlias

# Constants
ROOT_STR = "(root)"
FALLBACK_LOCALE = "en"
SEPARATOR = " → "
ALIAS_SEPARATOR = " or "  # For now at least, this isn't localized
BASE_PATH = Path("/System/Library")
UNKNOWN_PLACEHOLDER = "UNKNOWN_LABEL"
OVERRIDES = Path("./overrides")

# Folders in /System/Library/ known to contain bundles with Settings URLs
BUNDLE_LOCATIONS = (
	"BridgeManifests",
	"NanoPreferenceBundles",
	"PreferenceBundles",
	"PreferenceManifests",
	"PreferenceManifestsInternal"
)

# Keys for localizations that change based on device type
DEVICE_TYPES = ("iphone", "ipad", "ipod", "mac", "applevision", "other")

# TODO: use type statements once a-Shell upgrades to Python >=3.12
# type LocalizationString = str | dict[str, dict[str, str]]
LocalizationString: TypeAlias = str | dict[str, dict[str, str]]  # Only key in dict case is NSStringDeviceSpecificRuleType
# type NewOverride = dict[str, str | dict[str, str] | list[str]]
NewOverride: TypeAlias = dict[str, str | dict[str, str] | list[str]]

all_locales: set[str] = set()  # All the locales that have strings defined in localizations
# URLs that are wrong in the search manifests but can be easily remapped
# Currently supported: url, label_id
url_corrections: dict[str, dict[str, str]] = {}
alias_localizations: dict[str, dict[str, LocalizationString]] = {}  # If an alias is also a listed URL, let it inherit the localizations
# List of URLs that need to have new overrides created for them
urls_to_override: list[NewOverride] = []

def build_url(segments: Iterable[str]) -> str:
	"""
	Construct a Settings URL string from its path segments.
	:param segments: Iterable of segments to build the URL
	:return: Reconstructed URL
	"""
	segments_list = [*segments]
	if len(segments_list) == 0:
		return ""
	url_scheme = segments_list[0]
	constructed_url = url_scheme + ":"
	# TODO: if more schemes pop up, find a more general way to handle them.
	# So far, bridge and prefs are the same, and settings-navigation is the odd one out.
	# No other schemes seem to be around so far.
	if url_scheme == "settings-navigation":
		constructed_url += "//"
	if len(segments_list) > 1:
		url_fragment = segments_list[-1] if segments_list[-1].startswith("#") else None
		if url_fragment is not None:
				segments_list.pop()
		if url_scheme == "settings-navigation":
			constructed_url += quote("/".join(segments_list[1:]))
			if url_fragment is not None:
				constructed_url += url_fragment
		else:
			constructed_url += "root=" + segments_list[1]
			if len(segments_list) > 2:
				constructed_url += "&path=" + quote("/".join(segments_list[2:]))
		if url_fragment is not None:
			constructed_url += url_fragment
	return constructed_url


def get_path_segments(url: str) -> Generator[str, None, None]:
	"""
	Iterate the path segments in a Settings URL.
	:param url: URL to split
	:return: Generator that yields the path segments.
	"""
	parsed = urlparse(url, allow_fragments=True)
	yield parsed.scheme
	if len(parsed.netloc) > 0:
		parsed_netloc = parse_qs(parsed.netloc)
		if parsed_netloc:  # netloc is the root=...&path=...part, as in prefs: and bridge:
			if "root" in parsed_netloc:
				yield from parsed_netloc["root"]
			if "path" in parsed_netloc:
				for path_part in parsed_netloc["path"]:
					for seg in path_part.split("/"):
						if len(seg) > 0:
							yield seg
		else:  # netloc is just part of the path, like a domain, as in settings-navigation://
			yield parsed.netloc
		for seg in parsed.path.split("/"):
			if len(seg) > 0:
				yield seg
	else:
		parsed_path = parse_qs(parsed.path)
		if "root" in parsed_path:
			yield from parsed_path["root"]
		if "path" in parsed_path:
			for path_part in parsed_path["path"]:
				for seg in path_part.split("/"):
					if len(seg) > 0:
						yield seg
	if len(parsed.fragment) > 0:
		yield "#" + parsed.fragment  # prefix with "#" avoid possible collisions with "real" paths


def sanitize_key(key: LocalizationString) -> str:
	"""
	Flatten a localization string entry into one string, handling the special cases where a label differs between devices.

	Prefer iPhone where possible, otherwise get whichever available device type appears first in DEVICE_TYPES.
	
	Without this, we'd be trying to use a dict as a key in another dict, when we need a string.
	:param key: String or dictionary to turn into a single string.
	:return: String key
	"""
	if type(key) is str:
		return key
	elif type(key) is dict and "NSStringDeviceSpecificRuleType" in key:
		device_specific_labels: dict[str, str] = key["NSStringDeviceSpecificRuleType"]
		for device_type in DEVICE_TYPES:
			if device_type in device_specific_labels:
				return device_specific_labels[device_type]
	return UNKNOWN_PLACEHOLDER  # Either unknown label, or unrecognized device type


def merge_into(dictionary: dict, key: str, value: str | dict | list):
	"""
	Sets a value in a localized URL tree, using lists to allow URLs with the same human-readable path to stay alongside each other.
	This is useful to allow a single localized path to map to multiple URLs,
	because previously set URLs for that localized path will not be overwritten.
	:param dictionary: Dictionary to merge the value into.
	:param key: Key to set or merge the value for.
	:value: Value to merge into the dictionary.
	"""
	if key in dictionary:
		# Most of the time, the condition above will be false, but there may be locations with multiple URLs.
		# "I like spaghetti." Time to run Currahee. Now here's three miles up and down of insane code.
		existing_value = dictionary[key]
		if existing_value == value:  # Avoid adding duplicate entries for the same value. Python compares element-wise.
			return
		if type(existing_value) is str:
			if type(value) is dict:
				dictionary[key] = value
				merge_into(value, ROOT_STR, existing_value)
			else:
				if type(value) is str:
					urls_list = [existing_value, value]
					urls_list.sort()
					dictionary[key] = urls_list
				elif type(value) is list:
					value.append(existing_value)
					value.sort()
					dictionary[key] = value
		elif type(existing_value) is list:
			if type(value) is dict:
				dictionary[key] = value
				merge_into(value, ROOT_STR, existing_value)
			else:
				if type(value) is str:
					existing_value.append(value)
				elif type(value) is list:
					existing_value += value
				existing_value.sort()
		elif type(existing_value) is dict:
			if type(value) is dict:
				for key, sub_value in value.items():
					merge_into(existing_value, key, sub_value)
			else:
				merge_into(existing_value, ROOT_STR, value)
	else:
		dictionary[key] = value



class RawSettingsURL:
	"""
	A Settings URL associated with all of its available localized labels.
	"""
	def __init__(self, url: str, label_id: str, manifest_path: Path):
		# Apply corrections before any further processing
		self.url = url_corrections[url].get("url", url) if url in url_corrections else url
		self.label_id = url_corrections[url].get("label_id", label_id) if url in url_corrections else label_id
		self.manifest_path = manifest_path
		self.localized_labels: dict[str, LocalizationString] = {}
		self.aliases: list[str] | None = None

	def add_alias(self, url: str):
		if self.aliases is None:
			self.aliases = []
		self.aliases.append(url)
		alias_localizations[url] = self.localized_labels
		override_idx = 0
		# If any overrides would be created with the same URL as this alias, then delete them
		# We do this here, which may be in a recursively automatically generated alias,
		# to get all aliases
		while override_idx < len(urls_to_override):
			override = urls_to_override[override_idx]
			if override["url"] == url:
				del urls_to_override[override_idx]
			else:
				override_idx += 1


class Manifest:
	"""
	A collection of URLs that are stored together in a SettingsSearchManifest file.
	Each URL object is responsible for its own localizations.
	"""
	def __init__(self, path: Path):
		self.path = path
		self.urls: list[RawSettingsURL] = []
		self.strings: dict[str, dict[str, LocalizationString]] = {}

	def load(self):
		"""
		Load the URLs for this manifest and create an object for each one.
		"""
		# Load the actual URLs here. Delegate loading strings to the bundle.
		# Overrides will be handled externally.
		with open(self.path, "rb") as ssm:
			self.urls.extend(RawSettingsURL(plist_url["searchURL"], plist_url["label"], self.path) for plist_url in load_plist(ssm)["items"])


class Bundle:
	"""
	A collection of manifests and their localizations.
	"""
	def __init__(self, path: Path):
		self.path = path
		self.manifests: dict[str, Manifest] = {}  # key = SettingsSearchManiifest-whatever file name without .plist
		# Keys in lproj_strings and loctables, from highest to lowest:
		# - Manifest name (without)
		# - Locale identifier
		# - String identifier
		# - Optional: device-specific string top level key
		#   - Device type identifier
		self.lproj_strings: dict[str, dict[str, dict[str, LocalizationString]]] = {}
		self.loctables: dict[str, dict[str, dict[str, LocalizationString]]] = {}

	def load(self):
		"""
		Load the URLs and localizations from the files in the bundle.
		"""
		for file in self.path.iterdir():
			if file.is_file() and file.name.startswith("SettingsSearchManifest"):
				if file.name.endswith(".plist"):
					# Create a manifest
					manifest = Manifest(file)
					self.manifests[str(file).removesuffix(".plist")] = manifest
					manifest.load()
				elif file.name.endswith(".loctable"):
					# Load loctable
					with open(file, "rb") as loctable_file:
						loctable_contents: dict[str, dict[str, LocalizationString]] = load_plist(loctable_file)
					# For this purupose, LocProvenance is not useful, so delete it and pretend it never existed
					if "LocProvenance" in loctable_contents:
						del loctable_contents["LocProvenance"]
					# Don't save strings to their manifests right away,
					# because we need to preprocess them first (merge lproj and loctable)
					# and also because the manifest may not exist yet,
					# if the loctable or lproj is loaded before the actual manifest.
					self.loctables[str(file).removesuffix(".loctable")] = loctable_contents
				elif file.name.endswith(".strings"):
					# This is a special case, where there's a single strings file alongside the plist.
					# Since a strings file only has one level, there's only one localization language.
					# Assume it'll be English. All locales will end up using these translations.
					all_locales.add(FALLBACK_LOCALE)
					en_loc_dict = self.lproj_strings.setdefault(str(file).removesuffix(".strings"), {}).setdefault(FALLBACK_LOCALE, {})
					with open(file, "rb") as strings_file:
						en_loc_dict.update(load_plist(strings_file))
			elif file.is_dir() and file.name.endswith(".lproj"):
				# Load lproj
				# Reasoning for why the strings aren't saved to the manifest directly
				# is the same as for loctable logic
				lang = file.name.removesuffix(".lproj")
				all_locales.add(lang)
				for lproj_file in file.iterdir():
					if lproj_file.is_file() and lproj_file.name.startswith("SettingsSearchManifest") and lproj_file.name.endswith(".strings"):
						loc_dict = self.lproj_strings.setdefault(str(lproj_file).removesuffix(".strings"), {}).setdefault(lang, {})
						with open(lproj_file, "rb") as lproj_plist:
							loc_dict.update(load_plist(lproj_plist))

		# Now assign labels to URLs
		for manifest_name, manifest in self.manifests.items():
			# Merge loctables and lprojs
			manifest_strings: dict[str, dict[str, LocalizationString]] = {}
			if manifest_name in self.lproj_strings:
				manifest_strings.update(self.lproj_strings[manifest_name])
			if manifest_name in self.loctables:
				manifest_strings.update(self.loctables[manifest_name])
			# Save strings to their manifests to allow efficiently labeling overrides later
			manifest.strings = manifest_strings
			# Assign localized labels to URLs
			for url in manifest.urls:
				for locale, locale_strs in manifest_strings.items():
					if url.label_id in locale_strs:
						# URLs with localized labels
						if len(locale_strs[url.label_id]) == 0:
							raise Exception(f"Empty label for URL {url.url}, locale {locale}. Please adjust overrides as necessary.")
						url.localized_labels[locale] = locale_strs[url.label_id]

		# Now that labels are all assigned, clear the label dictionaries - we don't need them anymore
		# Hopefully this helps reduce memory consumption a tiny bit
		# This does not remove them from their manifests, however
		self.lproj_strings.clear()
		self.loctables.clear()


class URLTree:
	def __init__(self):
		self.root: RawSettingsURL | None = None
		self.urls: dict[str, URLTree] = {}

	def build_localized_tree(self, locale: str) -> dict | str | list[str]:
		"""
		Builds a localized sub-dictionary of URLs for export.
		:param locale: Locale
		:return: Dictionary of URLs with localized labels, or just the URL.
		"""
		if len(self.urls) == 0 and self.root is not None:
			# The root URL is the only thing available for this sub-path
			# Apply aliases -- return a list instead of a string
			if self.root.aliases is not None:
				return [self.root.url, *self.root.aliases]
			return self.root.url
		else:
			result = {}
			unassigned_id = 0
			if self.root is not None:
				merge_into(result, ROOT_STR, self.root.url)
			for subtree in self.urls.values():
				if subtree.root is not None:
					key_locale = locale if locale in subtree.root.localized_labels else FALLBACK_LOCALE
					if key_locale in subtree.root.localized_labels:
						key = sanitize_key(subtree.root.localized_labels[key_locale])
					else:
						key = f"{UNKNOWN_PLACEHOLDER}_{unassigned_id}"
						unassigned_id += 1
				else:
					key = f"{UNKNOWN_PLACEHOLDER}_{unassigned_id}"
					unassigned_id += 1
				merge_into(result, key, subtree.build_localized_tree(locale))
			return result

	def build_markdown_lines(self, locale: str, prefix: str = "- ", should_label: bool = False) -> Generator[str, None, None]:
		"""
		Build a localized Markdown list of URLs for export.
		:param locale: Locale
		:param prefix: Everything to come at the start of the human-readable left side of the line.
		:param should_label: Whether this tree's root node has a label. If false, then this is the root of the tree for an entire scheme (bridge or prefs).
		:return: Generator that yields a full Markdown list of the URLs in this subtree.
		"""
		if self.root is not None:
			locale_key = locale if locale in self.root.localized_labels else FALLBACK_LOCALE
			if locale_key in self.root.localized_labels:
				label = sanitize_key(self.root.localized_labels[locale_key])
			else:
				label = UNKNOWN_PLACEHOLDER
				# Print usages of the fallback label. They need to be addressed before publishing.
				# This does not trigger when overrides are needed to fill in gaps.
				if locale == FALLBACK_LOCALE:
					print("UNKNOWN PLACEHOLDER USED")
					print("  URL: " + self.root.url)
					print("  Manifest: " + str(self.root.manifest_path))
					print("  Label ID: " + self.root.label_id)
			prefix += label
			# Apply aliases if any exist
			urls_for_line = [self.root.url]
			if self.root.aliases is not None:
				urls_for_line += self.root.aliases
			urls_str = ALIAS_SEPARATOR.join(f"`{line_url}`" for line_url in urls_for_line)
			yield f"{prefix}: {urls_str}"
		elif should_label or len(prefix) > 2:  # Don't add to the label if this tree is for the entire URL scheme
			prefix += UNKNOWN_PLACEHOLDER
		if should_label:
			prefix += SEPARATOR
		# Generate the Markdown lines for child URLs
		for child in self.urls.values():
			yield from child.build_markdown_lines(locale, prefix, True)

	def add_url(self, url: RawSettingsURL):
		"""
		Add a URL to the tree. This instance of URLTree must be the root of the tree.
		:param url: Settings URL object to add to the tree
		"""
		current_tree = self
		# Removed the enumerate thing, will add back only if absolutely needed
		for path_segment in get_path_segments(url.url):
			if path_segment not in current_tree.urls:
				current_tree.urls[path_segment] = URLTree()
			current_tree = current_tree.urls[path_segment]
		# Now current tree is the subtree for which the provided URL is the root
		current_tree.root = url

	def add_alias(self, url: str, alias: str, recursive: bool, path_segments_rev: list[str] | None = None):
		"""
		Add an alias to a URL in this tree
		:param url: Original URL to create an alias for
		:param alias: Alias for the original URL
		:param recursive: Whether this alias can also be used as a prefix for children of the main URL
		"""
		if self.root is not None and self.root.url == url:
			self.root.add_alias(alias)  # Add alias to the URL directly
			# Apply to children if recursive
			# This isn't particularly efficient, I suppose, but it has to work.
			if recursive:
				# Generate this again
				aliased_url_segments = list(get_path_segments(url))
				alias_segments = list(get_path_segments(alias))
				for url_key, child_tree in self.urls.items():
					aliased_url_segments.append(url_key)
					alias_segments.append(url_key)
					# Add alias to the child tree
					child_tree.add_alias(build_url(aliased_url_segments), build_url(alias_segments), recursive)
					alias_segments.pop()
					aliased_url_segments.pop()  # Prepare segments list for the next child
		else:
			if path_segments_rev is None:
				# I am reversing them now to make popping more efficient as the recursion goes deeper into the tree
				# Otherwise I have to do .pop(0) which (AFAIK) is O(n)
				# This way I can just do .pop() which (again AFAIK) is O(1)
				path_segments_rev = list(get_path_segments(url))
				path_segments_rev.reverse()
			next_segment = path_segments_rev.pop()
			if next_segment in self.urls:
				# This will stop silently if the alias is trying to be applied to something that doesn't exist
				self.urls[next_segment].add_alias(url, alias, recursive, path_segments_rev)

	def find_missing(self, segments: list[str] | None = None) -> Generator[tuple[str, Self], None, None]:
		"""
		Find the missing URLs in the tree.
		:param segments: Settings URL segments needed to reach the root of this URL tree
		"""
		if segments is None:
			segments = []
		if self.root is None and len(segments) > 1:  # Ignore root of tree and URL schemes
			yield (build_url(segments), self)
		for key, subtree in list(self.urls.items()):
			segments.append(key)
			# This next line makes Pyright unhappy. Silence the error.
			# "Generator[tuple[str, URLTree], None, Unknown]" is not assignable to "Generator[tuple[str, Self@URLTree], None, None]"
			yield from subtree.find_missing(segments)  # pyright: ignore
			segments.pop()


# Read Settings URL manifests and build a tree
tree = URLTree()
manifests: dict[str, Manifest] = {}


def load_bundle(bundle_path: Path):
	"""
	Load a bundle and add its URLs to the tree.
	:param bundle_path: Path to the bundle
	"""
	bundle = Bundle(bundle_path)
	bundle.load()
	for bundle_manifest_id, bundle_manifest in bundle.manifests.items():
		manifests[bundle_manifest_id] = bundle_manifest
		for bundle_manifest_url in bundle_manifest.urls:
			tree.add_url(bundle_manifest_url)


def scan_folder(folder_path: Path):
	"""
	Scan a folder recursively for Settings URLs and load bundles if found.
	:param folder_path: Path to the folder to scan
	"""
	for file in folder_path.iterdir():
		if file.is_dir():
			if file.name.endswith(".bundle"):
				load_bundle(file)
			elif file.name != "_CodeSignature":
				scan_folder(file)


# Load the corrections before scanning the system.
# Corrections will be applied as soon as the affected URLs are loaded from the manifests.
with open(OVERRIDES / "corrections.json") as fp:
	url_corrections.update(load_json(fp))


# Load all bundles at known locations
for bundle_location in BUNDLE_LOCATIONS:
	scan_folder(BASE_PATH / bundle_location)
# One known special case that isn't in a normal bundle
load_bundle(BASE_PATH / "PrivateFrameworks" / "PBBridgeSupport.framework")


# Need both "fill-in-the-gap" overrides and "additional" overrides
# They operate on basically the same principle
# However, "fill-in-the-gap" overrides should only be used if they are actually needed
# That is, if none of their children are in the tree already, then they should not be added
# Manual insertion ("add") overrides have the same structure but are added first regardless
# Override structure:
# {
#   "url": str,
#   "label_id": str | None,  # mutually exclusive with label
#   "manifest": str | None,  # required for label_id only
#   "label": dict[str, str] | None  # mutually exclusive with label_id
# }
def add_override(override: dict):
	"""
	Add an override URL to the tree.
	:param override: JSON representation of the override
	"""
	# Bundle path is manifest 
	override_label_id = override.get("label_id")
	manifest_path_str = (override["manifest"] + ".plist") if "manifest" in override else ""
	override_url = RawSettingsURL(override["url"], "" if override_label_id is None else override_label_id, Path(manifest_path_str)) # Label ID doesn't matter
	if type(override_label_id) is str:
		# Use label_id to get the localizations from the manifest
		override_url_manifest = manifests[override["manifest"]]
		for lang, lang_localizations in override_url_manifest.strings.items():
			# Only add the localized label IDs that actually exist in the manifest's strings
			# All other uses will use the fallback
			if override_label_id in lang_localizations:
				override_url.localized_labels[lang] = lang_localizations[override_label_id]
	else:
		override_url.localized_labels = override["label"]  # Assume that one of label_id or label is guaranteed to exist
	tree.add_url(override_url)


# Inject "additional" overrides regardless of what's found
with open(OVERRIDES / "add.json", "r") as fp:
	additional_overrides: list[dict] = load_json(fp)
for additional_override in additional_overrides:
	add_override(additional_override)

# Create a folder for the current iOS version under versions/
# Doing this first so that the file containing the needed overrides has somewhere to go
with open("/System/Library/CoreServices/SystemVersion.plist", "rb") as fp:
	ios_version: str = load_plist(fp)["ProductVersion"]
version_folder = Path(".") / "versions" / ios_version
makedirs(version_folder, exist_ok=True)

# Record which URLs need manual overrides
# First, read which ones we want to ignore. These should never show up in a list of URLs.
with open(OVERRIDES / "ignore.txt", "r") as fp:
	ignored_urls = set(fp.read().strip().splitlines())


# Load gap overrides
# These are only applied on an as-needed basis,
# after the tree has been constructed.
# Each gap override has the same structure as an "additional" override.
with open(OVERRIDES / "gaps.json", "r") as fp:
	gap_overrides: list[NewOverride] = load_json(fp)


# Instead of a plain text file listing the URLs, we'll create a skeleton JSON.
# This should make the process of filling in localizations by hand less tedious.
def find_missing_urls():
	for missing_url_str, missing_url_tree in tree.find_missing():
		# Don't cry wolf on anything listed as ignored for overrides (original case: prefs:root=ROOT)
		if missing_url_str not in ignored_urls:
			# If the missing URL was already used as an alias, then inherit the label from the equivalent URL
			# In that case, it doesn't need an override at all
			if missing_url_str in alias_localizations:
				missing_url_tree.root = RawSettingsURL(missing_url_str, "", Path(""))
				missing_url_tree.root.localized_labels = alias_localizations[missing_url_str]
				continue
			# Either add missing URL from gaps.json, or add it to the list of overrides that need to be created
			found_gap_override = False
			for gap_override in gap_overrides:
				if gap_override["url"] == missing_url_str:
					found_gap_override = True
					# If label is manually specified instead of using label_id,
					# then use the URL as the label identifier.
					add_override(gap_override)
			if not found_gap_override:
				# Write override template for this URL to the needs-overrides file
				new_override: NewOverride = { "url": missing_url_str }
				found_similar_child = False
				if missing_url_tree.urls:  # empty dictionary is falsy
					for subtree in missing_url_tree.urls.values():
						if subtree.root is not None:
							# In many cases, the URLs that need overrides have the same last path segment and fragment.
							# The label will be identical; the fragment is the top of the page.
							# Therefore, we can automatically localize those without having to do all that manually.
							# Other special cases that we can handle here and save a bit of manual labor doing:
							# - #NumericalPreferenceSwitcherIdentifier
							# - #NumericalPreferencePickerGroupIdentifier
							# This isn't a great heuristic for where the manifest with the real label is, depending on the area.
							# But for now, it doesn't need to be any better.
							new_override["manifest"] = str(subtree.root.manifest_path).removesuffix(".plist")
							override_url_segments = list(get_path_segments(subtree.root.url))
							last_segment = override_url_segments[-1]
							second_last_segment = override_url_segments[-2]
							if last_segment == second_last_segment or last_segment == "#" + second_last_segment or last_segment == "#NumericalPreferenceSwitcherIdentifier" or last_segment == "#NumericalPreferencePickerGroupIdentifier":
								new_override["label_id"] = subtree.root.label_id
								# Don't even bother writing out overrides if they can be auto-populated.
								# Add them to the tree directly, since I'm fairly confident that they're correct.
								add_override(new_override)
								found_similar_child = True
								break
				if not found_similar_child:  # Only write out the ones that we couldn't automatically generate overrides for.
					# Default manifest (where many of these are found) if one could not be found in immediate children
					if "manifest" not in new_override:
						new_override["manifest"] = "/System/Library/PreferenceManifestsInternal/AccessibilitySettingsSearch.bundle/SettingsSearchManifest-com.apple.AccessibilitySettings"
					new_override["label_id"] = ""
					new_override["label"] = { "en": "" }
					# Just for me: a list of the immediate children of this URL, as hints for manual search
					child_url_hints = { k: v.root.label_id for k, v in missing_url_tree.urls.items() if v.root is not None }
					if child_url_hints:
						new_override["child_urls"] = child_url_hints
					urls_to_override.append(new_override)

# First search for missing items, before we add aliases
find_missing_urls()

# Load and add aliases to the tree (like prefs:root=CASTLE)
# Aliases file structure:
# {
#   "original_url": {
#     "recursive": bool,
#     "aliases": list[str]
#   }
# }
# I'm doing this after load the gaps and not before,
# because if a gap URL has an alias (e.g. prefs:root=APPLE_ACCOUNT),
# then applying the alias will throw an exception due to popping from an empty list.
# So we actually need to run two searches to make sure everything works out correctly.
# I hate running the search twice, but it just needs to work.
with open(OVERRIDES / "alias.json", "r") as fp:
	alias_overrides: dict[str, dict] = load_json(fp)
for orig_url, aliases_info in alias_overrides.items():
	aliases_for_url: list[str] = aliases_info["aliases"]
	aliases_are_recursive: bool = aliases_info["recursive"]
	for alias in aliases_for_url:
		# Yes, this does some unnecessary traversal of the tree.
		# It just has to work, though.
		tree.add_alias(orig_url, alias, aliases_are_recursive)

# Now that the aliases are applied, run the search for missing URLs again.
# This will be what we actually write to the JSON.
urls_to_override.clear()
find_missing_urls()


# Copy the relevant source files out of /System/Library for easier inspection,
# to speed up the process of manually creating and reviewing overrides.
# This is not strictly necessary, and the files should never be committed.
# It's just a convenience for me.
manifests_for_manual_search: set[str] = set()
for needed_override in urls_to_override:
	override_manifest = needed_override["manifest"]
	if type(override_manifest) is str and override_manifest not in manifests_for_manual_search:
		# Copy manifest and the localization (just EN if it's the .lproj format) to version folder for easy transfer
		manifest_name = Path(override_manifest).name
		manifest_loctable_original = Path(override_manifest + ".loctable")
		manifest_en_lproj_original = Path(override_manifest) / ".." / "en.lproj" / (manifest_name + ".strings")
		copy(override_manifest + ".plist",  version_folder / (manifest_name + ".plist"))
		if manifest_loctable_original.exists():
			copy(manifest_loctable_original, version_folder / (manifest_name + ".loctable"))
		elif manifest_en_lproj_original.exists():
			copy(manifest_en_lproj_original, version_folder / (manifest_name + ".strings"))
		manifests_for_manual_search.add(override_manifest)  # Don't copy the file again


# Write out the overrides that need manual investigation.
if len(urls_to_override) > 0:
	with open(version_folder / "need-overrides.json", "w") as fp:
		dump_json(urls_to_override, fp, indent=2)
	print(f"{len(urls_to_override)} override{'' if len(urls_to_override) == 1 else 's'} needed")
else:
	print("No overrides needed")


# Move anything under prefs:root=ROOT to the root level for building the localized tree.
# If this needs to be generalized later, so be it. For now, this is a special case.
# Currently, only Airplane Mode (prefs:root=ROOT#AIRPLANE_MODE) needs this.
prefs_scheme = tree.urls.get("prefs")  # prefs:
if prefs_scheme is not None:
	root_root = prefs_scheme.urls.get("ROOT")  # prefs:root=ROOT
	if root_root is not None:
		prefs_scheme.urls.update(root_root.urls)
		del prefs_scheme.urls["ROOT"]

# Build and export JSON and Markdown lists for all locales
for locale_code in all_locales:
	locale_folder = version_folder / locale_code
	makedirs(locale_folder, exist_ok=True)
	# Separate schemes into their own files for the fully localized area.
	for scheme, scheme_subtree in tree.urls.items():
		localized_tree = scheme_subtree.build_localized_tree(locale_code)
		json_path = locale_folder / f"{scheme}.json"
		with open(json_path, "w") as fp:
			dump_json(localized_tree, fp, indent=None)
		md_path = locale_folder / f"{scheme}.md"
		with open(md_path, "w") as fp:
			fp.write("\n".join(scheme_subtree.build_markdown_lines(locale_code)))

# For the top-level MD, JSON, and sorted JSON, include things from prefs and settings-navigation
# since settings-navigation seems to be on the rise in iOS 26.
# Build the combined settings URL tree
combined_settings_tree = URLTree()
# prefs is the primary scheme, at least for now
combined_settings_tree.urls.update(tree.urls["prefs"].urls)
for url_key, url_subtree in tree.urls["settings-navigation"].urls.items():
	if url_subtree.root is not None and url_subtree.root.url in alias_localizations:
		# If this URL was already added as an alias, don't add it to the main tree.
		# It'll just be a duplicate.
		continue
	combined_settings_tree.urls[url_key] = url_subtree

# Save the combined Markdown list and JSONs
with open("./settings-urls.md", "w") as fp:
	fp.write("\n".join(combined_settings_tree.build_markdown_lines(FALLBACK_LOCALE)))
localized_tree = combined_settings_tree.build_localized_tree(FALLBACK_LOCALE)
with open("./settings-urls.json", "w") as fp:
	dump_json(localized_tree, fp, indent=None)
with open("./settings-urls-sorted.json", "w") as fp:
	dump_json(localized_tree, fp, sort_keys=True, indent=4)