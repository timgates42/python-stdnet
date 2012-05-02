'''An object relational mapper library for Redis remote data structures.'''
VERSION = (0, 7, 0, 'rc', 1)

sphinxtogithub = False

from .exceptions import *
from .backends import *
from .utils.version import get_version 
 
__version__ = get_version(VERSION)
__license__ = "BSD"
__author__ = "Luca Sbardella"
__contact__ = "luca.sbardella@gmail.com"
__homepage__ = "https://github.com/lsbardel/python-stdnet"
CLASSIFIERS = [
               'Development Status :: 4 - Beta',
               'Environment :: Plugins',
               'Environment :: Console',
               'Environment :: Web Environment',
               'Intended Audience :: Developers',
               'License :: OSI Approved :: BSD License',
               'Operating System :: OS Independent',
               'Programming Language :: Python',
               'Programming Language :: Python :: 2.6',
               'Programming Language :: Python :: 2.7',
               'Programming Language :: Python :: 3.1',
               'Programming Language :: Python :: 3.2',
               'Programming Language :: Python :: 3.3',
               'Topic :: Utilities',
               'Topic :: Database',
               'Topic :: Internet'
               ]


