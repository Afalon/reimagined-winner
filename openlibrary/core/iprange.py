"""Tools to parse ip ranges.
"""
import re
import iptools

import six


four_octet = r'(\d+\.\d+\.\d+\.\d+)'
re_range_star = re.compile(r'^(\d+\.\d+)\.(\d+)\s*-\s*(\d+)\.\*$')
re_three = re.compile(r'^(\d+\.\d+\.\d+)\.$')
re_four = re.compile(r'^' + four_octet + r'(/\d+)?$')
re_range_in_last = re.compile(r'^(\d+\.\d+\.\d+)\.(\d+)\s*-\s*(\d+)$')
re_four_to_four = re.compile('^%s\s*-\s*%s$' % (four_octet, four_octet))

patterns = (re_four_to_four, re_four, re_range_star, re_three, re_range_in_last)

def parse_ip_ranges(text):
    """Parses IP ranges in various formats from a multi-line text and returns them in standard representation.

    Text after # is considered as comment. Comments and empty lines are ignored.

    Supported formats:

    "1.2.3.4" -> "1.2.3.4"
    "1.2.3 - 4.*" -> ("1.2.3.0", "1.2.4.255")
    "1.2.3.4-10" -> ("1.2.3.4", "1.2.3.10")
    "1.2.3.4 - 2.3.4.5" -> ("1.2.3.4", "2.3.4.5")
    "1.2.*.* "-> "1.2.0.0/16"
    """
    for line in text.splitlines():
        # strip comments
        line = line.split("#")[0].strip()

        # ignore empty lines
        if not line:
            continue

        # accept IPs
        m = re_four.match(line)
        if m:
            yield line
            continue

        # accept IP ranges
        m = re_range_star.match(line)
        if m:
            start = '%s.%s.0' % (m.group(1), m.group(2))
            end = '%s.%s.255' % (m.group(1), m.group(3))
            yield (start, end)
            continue

        # consider 1.2.3 as 1.2.3.0 - 1.2.3.255
        m = re_three.match(line)
        if m:
            yield ('%s.0' % m.group(1), '%s.255' % m.group(1))
            continue

        # consider 1.2.3.4-10 as 1.2.3.4 - 1.2.3.10
        m = re_range_in_last.match(line)
        if m:
            yield ('%s.%s' % (m.group(1), m.group(2)), '%s.%s' % (m.group(1), m.group(3)))
            continue

        # accept 1.2.3.4 - 2.3.4.5
        m = re_four_to_four.match(line)
        if m:
            yield m.groups()
            continue

        # consider 1.2.*.* as 1.2.0.0/16
        if '*' in line:
            collected = []
            octets = line.split('.')
            while octets[0].isdigit():
                collected.append(octets.pop(0))
            if collected and all(octet == '*' for octet in octets):
                yield '%s/%d' % ('.'.join(collected + ['0'] * len(octets)), len(collected) * 8)
            continue

def find_bad_ip_ranges(text):
    """Returns bad ip-ranges in the given text.

    Lines which don't match the supported IP range formats are considered bad.
    See :func:`parse_ip_ranges` for the list of supported ip-range formats.
    """
    bad = []
    for orig in text.splitlines():
        line = orig.split("#")[0].strip()
        if not line:
            continue
        if any(pat.match(line) for pat in patterns):
            continue
        if '*' in line:
            collected = []
            octets = line.split('.')
            while octets[0].isdigit():
                collected.append(octets.pop(0))
            if collected and all(octet == '*' for octet in octets):
                continue
        bad.append(orig)
    return bad

class IPDict:
    """Efficient dictionary of IP ranges to values.

    IP ranges can be added by calling :meth:`add_ip_range`. And values can be accessed by IP.

        >>> ipmap = IPRangeMap()
        >>> ipmap.add_ip_range("1.2.3.0/8", "foo")
        >>> ipmap['1.2.3.4']
        >>> 'foo'
        >>> ipmap.get('1.2.3.4')
        'foo'
        >>> ipmap.get('1.2.5.5')
    """
    def __init__(self):
        # 2-level Dictionary for storing IP ranges
        #
        # For efficient lookup, IP ranges are stored in 2-levels.
        # First key is the integer representation of first 2 parts of the ip
        # Second key in the IpRange object.
        #
        # When to look for an IP, the integer representation of the first 2
        # parts of the IP are used to get the IpRanges to check for.
        self.ip_ranges = {}

    def add_ip_range(self, ip_range, value):
        """Adds an entry to this map.

        ip_range can be in the following forms:

            "1.2.3.4"
            "1.2.3.0/8"
            ("1.2.3.4", "1.2.3.44")
        """
        # Convert ranges in CIDR format into (start, end) tuple
        if isinstance(ip_range, six.string_types) and "/" in ip_range:
            # ignore bad value
            if not iptools.ipv4.validate_cidr(ip_range):
                return
            ip_range = iptools.ipv4.cidr2block(ip_range)

        # Find the integer representation of first 2 parts of the start and end IPs
        if isinstance(ip_range, tuple):
            # ignore bad ips
            if not iptools.ipv4.validate_ip(ip_range[0]) or not iptools.ipv4.validate_ip(ip_range[1]):
                return

            # Take the first 2 parts of the begin and end ip as integer
            start = iptools.ipv4.ip2long(ip_range[0]) >> 16
            end = iptools.ipv4.ip2long(ip_range[1]) >> 16
        else:
            start = iptools.ipv4.ip2long(ip_range) >> 16
            end = start

        # for each integer in the range add an entry.
        for i in range(start, end+1):
            self.ip_ranges.setdefault(i, {})[iptools.IpRange(ip_range)] = value

    def add_ip_range_text(self, ip_range_text, value):
        """Adds all ip_ranges from the givem multi-line text and associate
        each one of them with given value.

        See :func:`parse_ip_ranges` for the supported formats in the text.
        """
        for ip_range in parse_ip_ranges(ip_range_text):
            self.add_ip_range(ip_range, value)

    def __getitem__(self, ip):
        # integer representation of first 2 parts
        base = iptools.ipv4.ip2long(ip) >> 16
        for ip_range, value in self.ip_ranges.get(base, {}).items():
            if ip in ip_range:
                return value
        raise KeyError(ip)

    def __contains__(self, ip):
        try:
            self[ip]
            return True
        except KeyError:
            return False

    def get(self, ip, default=None):
        try:
            return self[ip]
        except KeyError:
            return None
