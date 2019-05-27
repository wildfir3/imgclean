#!/usr/bin/env python

import cv2
from PIL import Image
import numpy as np
import sys
import os
from glob import glob
from collections import defaultdict
import argparse
import zlib

MIN_W, MIN_H = 1200, 1200*9.0/16
SIZE = (64, 36)  # 16:9
HASH_DIM = (8, 8)
HASH_SIZE = HASH_DIM[0] * HASH_DIM[1]
CACHE_FILE = 'fingerprint.db'
JUNK_FOLDER = '[Junk]'
DUPE_FOLDER = '[Dupes]'
SIMILARITY_THRESH = 8
SUPPORTED_IMAGE_CONTENT_FILE_EXTENSIONS = ['.jpg', '.jpeg', '.png', '.bmp', '.tiff', '.tif']
READ_BUFFER_SIZE = 65536


class FileInfo:
    def __init__(self, filepath):
        self.filepath = filepath
        self.phash = None
        self.width = None
        self.height = None
        self.crc32 = None

    def is_image(self):
        return os.path.splitext(self.filepath)[1].lower() in SUPPORTED_IMAGE_CONTENT_FILE_EXTENSIONS

    def _load_image(self):
        """Load an image and resize it with OpenCV"""
        cv2_load_method = 0
        if hasattr(cv2, 'CV_LOAD_IMAGE_GRAYSCALE'):
            cv2_load_method = cv2.CV_LOAD_IMAGE_GRAYSCALE
        elif hasattr(cv2, 'IMREAD_GRAYSCALE'):
            cv2_load_method = cv2.IMREAD_GRAYSCALE
        else:
            print 'Aborting. Your CV2 version does not appear to support loading images as greyscale.'
            exit()

        bytes = bytearray()
        with open(self.filepath, "rb") as stream:
            buf = stream.read(READ_BUFFER_SIZE)
            while len(buf) > 0:
                bytes += bytearray(buf)
                buf = stream.read(READ_BUFFER_SIZE)

        numpyarray = np.asarray(bytes, dtype=np.uint8)
        img = cv2.imdecode(numpyarray, cv2_load_method)
        if img is None:
            print 'failed to read image %s' % (self.filepath.encode('utf-8'))
            return None
        # store original height & width; to be used later to determine "best" copy of image
        self.height, self.width = img.shape

        try:
            img = cv2.resize(img, SIZE)
        except cv2.error, e:
            print 'Error loading %s' % (self.filepath.encode('utf-8'))
            raise e

        return img

    def compute_crc32(self):
        """Compute crc32 hash of a file"""
        with open(self.filepath, 'rb') as f:
            buf = f.read(READ_BUFFER_SIZE)
            crc_value = 0
            while len(buf) > 0:
                crc_value = zlib.crc32(buf, crc_value)
                buf = f.read(READ_BUFFER_SIZE)

        self.crc32 = format(crc_value & 0xFFFFFFFF, '08x')

    def _compute_dct(self, img):
        """Get the discrete cosine transform of an image"""
        return np.uint8(cv2.dct(np.float32(img)/255.0)*255)

    def compute_phash(self):
        """Compute a perceptual hash of an image"""
        if not self.is_image():
            return None

        img = self._load_image()
        if img is None:
            return None
        dct = self._compute_dct(img)
        dct = dct[:HASH_DIM[0], :HASH_DIM[1]]
        avg = np.average(dct)
        bits = [(x > avg) for x in dct.flatten()]
        self.phash = sum([2**i * int(bits[i]) for i in range(len(bits))])



def get_args():
    parser = argparse.ArgumentParser(description='Clean up image files', add_help=False, formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument('--help', action="help")
    #parser.add_argument('-r', '--rename-similar', action='store_true', help="Group together similar-looking images for easy removal")
    parser.add_argument('-r', '--recursive', action='store_true', help="Recurse into subfolders of the target folder")
    parser.add_argument('-s', '--remove-small', action='store_true', help="Move images smaller than a certain threshold to a separate directory")
    parser.add_argument('-d', '--move-suspected-duplicates', action='store_true', help="Move all suspected duplicates (including original) into a separate directory")
    parser.add_argument('-i', '--image-content', action='store_true', help="Scan for potential duplicate images by matching image content. This only operates with a subset of image files. This is the default option")
    parser.add_argument('-n', '--filename-match', action='store_true', help="Scan for potential duplicates by matching filenames. This supports all filetypes")
    parser.add_argument('-c', '--crc-match', action='store_true', help="Scan for potential duplicates by matching file CRC32. This supports all filetypes")
    parser.add_argument('-w', '--min-width', default=MIN_W, help="Minimum width")
    parser.add_argument('-h', '--min-height', default=MIN_H, help="Minimum height")
    parser.add_argument('-t', '--threshold', default=SIMILARITY_THRESH, help="Threshold below which images are too similar")
    parser.add_argument('folder', nargs='?', default='.', help="Folder to scan")

    return parser.parse_args()

def too_small(filename):
    """Test if an image file is too small"""
    try:
        img = Image.open(filename)
    except IOError:
        # Probably not an image
        return False
    w, h = img.size
    if w < MIN_W or h < MIN_H:
        return True


def hamming(h1, h2):
    """Compute the hamming distance (as binary strings) between two integers"""
    h, d = 0, h1 ^ h2
    while d:
        h += 1
        d &= d - 1
    return h


def amalgamate(amalgams):
    """Collapse a graph described by a dict into connected components"""
    def dfs(visited, component, current):
        try:
            for c in amalgams[current]:
                if c.filepath not in visited:
                    visited.add(c.filepath)
                    component.append(c)
                    dfs(visited, component, c)
        except KeyError:
            pass

        return component

    visited = set()
    components = {}
    for i in amalgams:
        if i.filepath not in visited:
            visited.add(i.filepath)
            components[i] = dfs(visited, [i], i)

    return components


def read_cache():
    try:
        fd = open(CACHE_FILE, 'r')
    except:
        raise ValueError('Could not open cache file')

    cache = {}
    for l in fd.readlines():
        line = l.strip().split('\t')
        try:
            cache[line[0].decode('utf-8')] = {'mtime': int(line[1]), 'phash': int(line[2]) if line[2] else None, 'width': int(line[3]) if line[3] else None, 'height': int(line[4]) if line[4] else None, 'crc32': line[5]}
        except:
            print 'Failed to read cache line: %s' % (line.encode('utf-8'))

    fd.close()
    return cache


def write_cache(fileinfos):
    if len(fileinfos) == 0:
        return

    try:
        fd = open(CACHE_FILE, 'w')
    except:
        raise ValueError('Could not open cache file for writing')

    for fileinfo in fileinfos:
        mtime = int(os.path.getmtime(fileinfo.filepath))
        fd.write('%s\t%s\t%s\t%s\t%s\t%s\n' % (fileinfo.filepath.encode('utf-8'), mtime, fileinfo.phash or '', fileinfo.width or '', fileinfo.height or '', fileinfo.crc32))

    fd.close()


def create_folder(name):
    if not os.path.exists(name):
        try:
            os.makedirs(name)
            print "Creating '%s' folder" % name.encode('utf-8')
        except OSError:
            print "Could not create '%s' folder" % name.encode('utf-8')
            sys.exit(1)
    elif not os.path.isdir(name):
        print "A file named '%s' exists and it is not a directory." % name.encode('utf-8')
        sys.exit(1)


def safely_move_file(fileinfo, target_filepath):
    target_file_directory = os.path.dirname(target_filepath)
    create_folder(target_file_directory)
    source_filepath, source_file_extension = os.path.splitext(fileinfo.filepath)

    rename_counter = 0
    while os.path.exists(target_filepath):
        print 'Asked to rename %s to %s but the latter already exists. Appending -%d' % (fileinfo.filepath.encode('utf-8'), target_filepath.encode('utf-8'), rename_counter)
        new_filename = '%s-%d%s' % (source_filepath, rename_counter, source_file_extension)
        target_filepath = os.path.join(target_file_directory, new_filename)
        rename_counter += 1
    os.rename(fileinfo.filepath, target_filepath)
    return target_filepath


if __name__ == '__main__':
    locals().update(vars(get_args()))

    try:
        os.chdir(folder)
    except OSError:
        print 'Invalid path: %s' % (folder.encode('utf-8'))
        sys.exit(1)
    # File operations are now relative to source directory
    
    print "Begin processing root image directory '%s'" % folder.encode('utf-8')

    directory_name = os.path.basename(os.path.normpath(folder))

    if remove_small:
        create_folder(JUNK_FOLDER)

    if move_suspected_duplicates:
        create_folder(DUPE_FOLDER)
        
    try:
        cache = read_cache()
    except ValueError:
        print 'Error reading cache file; ignoring'
        cache = {}

    _print_counter = 0
    def _print_progress(char):
        global _print_counter
        _print_counter += 1
        sys.stdout.write(char)
        if _print_counter > 80:
            sys.stdout.write("\n")
            sys.stdout.flush()
            _print_counter = 0

    chosen_duplicate_search_method = ''
    if filename_match:
        chosen_duplicate_search_method = 'filename match'
    elif crc_match:
        chosen_duplicate_search_method = 'CRC32'
    else:
        chosen_duplicate_search_method = 'image content'
        image_content = True #Searching for duplicates by image content is the default option

    # Scan files in target folder, gathering metadata to prepare to identify duplicates
    fileinfos = []
    keyed_file_list = defaultdict(list)
    for root, dir_list, file_list in os.walk(u'.'):
        # Exclude OS trash folders & the directories used by this script
        dir_list[:] = [d for d in dir_list if d not in (JUNK_FOLDER, DUPE_FOLDER, '$RECYCLE.BIN', '.Trash')]

        print "\nBegin scanning directory '%s':" % root.encode('utf-8')
        _print_counter = 0

        for filename in file_list:
            if filename == CACHE_FILE:
                continue

            filepath = os.path.join(root, filename)
            fileinfo = FileInfo(filepath)

            if image_content and not fileinfo.is_image():
                continue

            # Move the file away if it's too small
            if remove_small and too_small(filepath):
                try:
                    junk_file_path = os.path.join(JUNK_FOLDER, filepath)
                    junk_file_directory = os.path.dirname(junk_file_path)
                    create_folder(junk_file_directory)
                    os.rename(filepath, junk_file_path)
                    print 'Moving %s to junk as it is too small.' % (filepath.encode('utf-8'))
                except OSError, e:
                    print 'Failed to move %s: %s' % (filepath.encode('utf-8'), e)
                continue

            try:
                # Get cached info
                if cache[filepath]['mtime'] == int(os.path.getmtime(filepath)):
                    for var in ('phash', 'width', 'height', 'crc32'):
                        setattr(fileinfo, var, cache[filepath][var])
                else:
                    # update hash if file has been modified since cached result
                    fileinfo.compute_phash()
                    fileinfo.compute_crc32()
                    _print_progress('+') # + represents updating known item in cache
            except KeyError:
                # Compute hashes of uncached files
                fileinfo.compute_phash()
                fileinfo.compute_crc32()
                _print_progress('.') # . represents adding new item to cache

            if filename_match:
                keyed_file_list[filename].append(fileinfo)
            elif crc_match:
                keyed_file_list[fileinfo.crc32].append(fileinfo)

            fileinfos.append(fileinfo)
        print "done"
        sys.stdout.flush()

        if not recursive:
            break

    write_cache(fileinfos) # write out the current state to cache, in case we have any issues in the next step
    print '\nFinished scanning %s files in %s' % (len(fileinfos), folder.encode('utf-8'))
    print '\nBegin identifying duplicate files using the %s method\n' % (chosen_duplicate_search_method)
    sys.stdout.flush()

    if filename_match or crc_match:
        # filter out items which have no potential duplicates
        keyed_file_list = {k:v for k, v in keyed_file_list.iteritems() if len(v) > 1}
    else:
        # Find pairs of images whose phash is similar
        keyed_file_list = defaultdict(list) #throw away the simple key stuff we did earlier
        for i, file_a in enumerate(fileinfos):
            if file_a.phash is None:
                continue
            for file_b in fileinfos[i+1:]:
                if file_b.phash is None:
                    continue
                if hamming(file_a.phash, file_b.phash) < SIMILARITY_THRESH:
                    keyed_file_list[file_a].append(file_b)
                    keyed_file_list[file_b].append(file_a)

        # Group together all images which are similar
        keyed_file_list = dict(keyed_file_list)
        keyed_file_list = amalgamate(keyed_file_list)

    for similar in keyed_file_list.values():
        if image_content:
            # sort to prefer the largest (pixel area) image first
            similar.sort(key = lambda f: f.height * f.width, reverse = True)
        master_filepath = similar[0].filepath
        master_filepath_without_extension = os.path.splitext(master_filepath)[0]

        if move_suspected_duplicates:
            duplicate_master_filepath = os.path.join(DUPE_FOLDER, master_filepath)
            print 'Moving master file %s to %s' % (master_filepath.encode('utf-8'), duplicate_master_filepath.encode('utf-8'))

            new_duplicate_master_filepath = safely_move_file(similar[0], duplicate_master_filepath)
            master_filepath_without_extension = os.path.splitext(new_duplicate_master_filepath)[0]

            index_to_remove = [i for i, f in enumerate(fileinfos) if f.filepath == master_filepath][0]
            del fileinfos[index_to_remove]

        # Rename similar files to to be <name>.jpg, <name>_v1.jpg, <name>_v2.jpg etc
        for i, duplicate_fileinfo in enumerate(similar[1:], 1):
            duplicate_filepath = duplicate_fileinfo.filepath
            duplicate_file_extension = os.path.splitext(duplicate_filepath)[1]
            new_duplicate_filepath = '%s_v%d%s' % (master_filepath_without_extension, i, duplicate_file_extension)

            fileinfo_index_to_update = [i for i, f in enumerate(fileinfos) if f.filepath == duplicate_filepath][0]
            if move_suspected_duplicates:
                print 'Moving suspected duplicate %s to %s.' % (duplicate_filepath.encode('utf-8'), new_duplicate_filepath.encode('utf-8'))
                del fileinfos[fileinfo_index_to_update]
            else:
                print 'Renaming %s to %s due to similarities.' % (duplicate_filepath.encode('utf-8'), new_duplicate_filepath.encode('utf-8'))
                fileinfos[fileinfo_index_to_update].filepath = new_duplicate_filepath

            safely_move_file(duplicate_fileinfo, new_duplicate_filepath)

    write_cache(fileinfos)

    print '\nCleanup complete.'

