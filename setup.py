from setuptools import find_packages, setup

with open("requirements.txt") as f:
	install_requires = f.read().strip().split("\n")

# get version from __version__ variable in etraph_theme/__init__.py
from etraph_theme import __version__ as version

setup(
	name="etraph_theme",
	version=version,
	description="Habillage ETRAPH (couleurs, polices) pour ERPNext",
	author="ETRAPH",
	author_email="tk@etraph.com",
	packages=find_packages(),
	zip_safe=False,
	include_package_data=True,
	install_requires=install_requires,
)
