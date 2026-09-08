"""Join diagnostic timestamps without delaying perception messages."""

from collections import OrderedDict


def stamp_key(stamp):
    """Keep the original ROS timestamp as an exact observation key."""
    return (stamp.sec, stamp.nanosec)


class CameraTiming:
    """Bounded, order-independent join of YOLO receipt and 3D publication."""

    def __init__(self, capacity=120):
        """Bound pending diagnostic records independently of inference."""
        self.capacity = capacity
        self.receipts = OrderedDict()
        self.publications = OrderedDict()

    def received(self, trace):
        """Record the timestamp captured before upstream YOLO inference."""
        key = stamp_key(trace.source_stamp)
        self.receipts[key] = trace
        return self._join(key)

    def published(self, stamp, steady_ns):
        """Record the actual 3D output publication, never an estimate."""
        key = stamp_key(stamp)
        self.publications[key] = steady_ns
        return self._join(key)

    def _join(self, key):
        result = None
        if key in self.receipts and key in self.publications:
            trace = self.receipts.pop(key)
            trace.publish_steady_time_ns = self.publications.pop(key)
            result = trace
        for records in (self.receipts, self.publications):
            while len(records) > self.capacity:
                records.popitem(last=False)
        return result
