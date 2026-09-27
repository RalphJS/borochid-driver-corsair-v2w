Name:           borochid-driver-corsair-v2w
Version:        0.1.0
Release:        1%{?dist}
Summary:        Borochid driver for Corsair V2W wireless headsets
License:        Apache-2.0
URL:            https://github.com/RalphJS/borochid-driver-corsair-v2w
Source0:        %{name}-%{version}.tar.gz
BuildArch:      noarch

BuildRequires:  python3-devel
BuildRequires:  systemd-rpm-macros
# python3dist(borochid-service) is generated from pyproject.toml.

%description
Driver plugin for the Borochid peripheral service. Speaks Corsair's V2W HID
protocol (Virtuoso and related wireless headsets): RGB zones, battery and
charge state, hardware sidetone and the physical mic-mute button. Device
models are described by signed data packages from the Borochid registry;
this package provides the code and the udev access rule.

%prep
%autosetup -p1

%generate_buildrequires
%pyproject_buildrequires

%build
%pyproject_wheel

%install
%pyproject_install
%pyproject_save_files borochid_corsair_v2w
install -Dpm0644 udev/70-borochid-corsair-v2w.rules %{buildroot}%{_udevrulesdir}/70-borochid-corsair-v2w.rules

%check
%pyproject_check_import

%post
%udev_rules_update

%postun
%udev_rules_update

%files -f %{pyproject_files}
%license LICENSE NOTICE
%doc README.md
%{_udevrulesdir}/70-borochid-corsair-v2w.rules

%changelog
* Sun Sep 27 2026 Rodolfo Justiniano <rodolfo@beglaux.com> - 0.1.0-1
- Initial package
