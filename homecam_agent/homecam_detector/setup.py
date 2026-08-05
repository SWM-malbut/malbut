from setuptools import find_packages, setup

package_name = "homecam_detector"

setup(
    name=package_name,
    version="0.1.0",
    packages=find_packages(exclude=("test",)),
    data_files=[
        ("share/ament_index/resource_index/packages", [f"resource/{package_name}"]),
        (f"share/{package_name}", ["package.xml"]),
    ],
    install_requires=["setuptools"],
    zip_safe=True,
    maintainer="SWM Malbut contributors",
    maintainer_email="maintainers@example.com",
    description=(
        "On-device home-camera motion and person, dog, and cat event detector."
    ),
    license="Apache-2.0",
    tests_require=["pytest"],
    entry_points={
        "console_scripts": [
            "homecam_detector_node = homecam_detector.detector_node:main",
        ],
    },
)
