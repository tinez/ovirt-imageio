# ovirt-imageio
# Copyright (C) 2015-2016 Red Hat, Inc.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.

import logging
import threading
import urllib.parse as urllib_parse

from . import backends
from . import errors
from . import measure
from . import ops
from . import util

log = logging.getLogger("auth")


class Ticket:

    def __init__(self, ticket_dict, cfg):
        if not isinstance(ticket_dict, dict):
            raise errors.InvalidTicket(
                "Invalid ticket: %r, expecting a dict" % ticket_dict)

        self._uuid = _required(ticket_dict, "uuid", str)
        self._size = _required(ticket_dict, "size", int)
        self._ops = _required(ticket_dict, "ops", list)

        self._timeout = _required(ticket_dict, "timeout", int)
        self._inactivity_timeout = _optional(
            ticket_dict, "inactivity_timeout", int,
            default=cfg.daemon.inactivity_timeout)

        now = int(util.monotonic_time())
        self._expires = now + self._timeout
        self._access_time = now

        url_str = _required(ticket_dict, "url", str)
        try:
            self._url = urllib_parse.urlparse(url_str)
        except (ValueError, AttributeError, TypeError) as e:
            raise errors.InvalidTicketParameter("url", url_str, e)
        if not backends.supports(self._url.scheme):
            raise errors.InvalidTicketParameter(
                "url", url_str,
                "Unsupported url scheme: %s" % self._url.scheme)

        self._transfer_id = _optional(ticket_dict, "transfer_id", str)

        # Engine before 4.2.7 did not pass the transfer id. Generate a likey
        # unique value from the first half of the uuid.
        if self._transfer_id is None:
            self._transfer_id = f"(ticket/{self._uuid[:18]})"

        self._filename = _optional(ticket_dict, "filename", str)
        self._sparse = _optional(ticket_dict, "sparse", bool, default=False)
        self._dirty = _optional(ticket_dict, "dirty", bool, default=False)

        self._operations = []
        self._lock = threading.Lock()

        # Set holding ongoing operations.
        self._ongoing = set()

        # Ranges transferred by completed operations.
        self._completed = measure.RangeList()

        # Set to true when a ticket is canceled. Once canceled, all operations
        # on this ticket will raise errors.AuthorizationError.
        self._canceled = False

        # Mapping of connection id to connection context. When empty, this
        # ticket is not used by any connection.
        self._connections = {}

        # Used for waiting until a ticket is unused during cancellation. A
        # ticket can be removed only when this event is set.
        self._unused = threading.Event()

    @property
    def uuid(self):
        return self._uuid

    @property
    def size(self):
        return self._size

    @property
    def url(self):
        return self._url

    @property
    def ops(self):
        return self._ops

    @property
    def expires(self):
        return self._expires

    @property
    def transfer_id(self):
        """
        Return the ticket transfer id, available since engine 4.2.7 or None
        if the ticket was generated by older engine.
        """
        return self._transfer_id

    @property
    def filename(self):
        return self._filename

    @property
    def sparse(self):
        return self._sparse

    @property
    def dirty(self):
        """
        Return True if ticket's url should provide dirty extents information.
        """
        return self._dirty

    @property
    def idle_time(self):
        """
        Return the time in which the ticket became inactive.
        """
        if self.active():
            return 0
        return int(util.monotonic_time()) - self._access_time

    @property
    def inactivity_timeout(self):
        """
        Return the number of seconds to wait before disconnecting inactive
        client.
        """
        return self._inactivity_timeout

    @property
    def canceled(self):
        with self._lock:
            return self._canceled

    def add_context(self, con_id, context):
        with self._lock:
            if self._canceled:
                raise errors.AuthorizationError(
                    "Transfer {} was canceled".format(self.transfer_id))

            log.debug("Adding connection %s context to transfer %s",
                      con_id, self.transfer_id)
            self._connections[con_id] = context

    def get_context(self, con_id):
        return self._connections[con_id]

    def remove_context(self, con_id):
        with self._lock:
            try:
                context = self._connections[con_id]
            except KeyError:
                return

            log.debug("Removing connection %s context from transfer %s",
                      con_id, self.transfer_id)
            context.close()

            # If context was closed, it is safe to remove it.
            del self._connections[con_id]

    def run(self, operation):
        """
        Run an operation, binding it to the ticket.
        """
        self._add_operation(operation)
        try:
            return operation.run()
        except ops.Canceled:
            log.debug("Operation %s was canceled", operation)
        finally:
            self._remove_operation(operation)

    def touch(self):
        """
        Extend the ticket and update the last access time.

        Must be called when an operation is completed.
        """
        now = int(util.monotonic_time())
        self._expires = now + self._timeout
        self._access_time = now

    def _add_operation(self, op):
        with self._lock:
            if self._canceled:
                raise errors.AuthorizationError(
                    "Transfer {} was canceled".format(self.transfer_id))

            self._ongoing.add(op)

    def _remove_operation(self, op):
        with self._lock:
            self._ongoing.remove(op)

            if self._canceled:
                # If this was the last ongoing operation, wake up caller
                # waiting on cancel().
                if not self._ongoing:
                    log.debug(
                        "Removed last ongoring operation for transfer %s",
                        self.transfer_id)
                    self._unused.set()

                raise errors.AuthorizationError(
                    "Transfer {} was canceled".format(self.transfer_id))

            # We don't report transfered bytes for read-write ticket.
            if len(self.ops) == 1:
                r = measure.Range(op.offset, op.offset + op.done)
                self._completed.add(r)

        self.touch()

    def active(self):
        return bool(self._ongoing)

    def transferred(self):
        """
        The number of bytes that were transferred so far using this ticket.
        """
        if len(self.ops) > 1:
            # Both read and write, cannot report meaningful value.
            return None

        with self._lock:
            # NOTE: this must not modify the ticket state.
            completed = measure.RangeList(self._completed)
            ongoing = [measure.Range(op.offset, op.offset + op.done)
                       for op in self._ongoing]

        completed.update(ongoing)
        return completed.sum()

    def may(self, op):
        if op == "read":
            # Having "write" imply also "read".
            return "read" in self.ops or "write" in self.ops
        else:
            return op in self.ops

    def info(self):
        info = {
            "active": self.active(),
            "canceled": self._canceled,
            "connections": len(self._connections),
            "expires": self._expires,
            "idle_time": self.idle_time,
            "inactivity_timeout": self._inactivity_timeout,
            "ops": list(self._ops),
            "size": self._size,
            "sparse": self._sparse,
            "dirty": self._dirty,
            "timeout": self._timeout,
            "url": urllib_parse.urlunparse(self._url),
            "uuid": self._uuid,
        }
        if self._transfer_id:
            info["transfer_id"] = self._transfer_id
        if self.filename:
            info["filename"] = self.filename
        transferred = self.transferred()
        if transferred is not None:
            info["transferred"] = transferred
        return info

    def extend(self, timeout):
        expires = int(util.monotonic_time()) + timeout
        self._expires = expires

    def cancel(self, timeout=60):
        """
        Cancel a ticket and wait until all ongoing operations finish.

        Arguments:
            timeout (float): number of seconds to wait until the ticket is
                unused. If timeout is zero, return immediately without waiting.
                The caller will have to poll the ticket status until the number
                of connections becomes zero.

        Returns:
            True if ticket can be removed.

        Raises:
            errors.TicketCancelTimeout if timeout is non-zero and the ticket is
            still used when the timeout expires.
        """
        log.debug("Cancelling transfer %s", self.transfer_id)

        with self._lock:
            # No operation can start now, and new connections cannot be added
            # to the ticket.
            self._canceled = True

            if not self._ongoing:
                # There are no ongoing opearations, but we may have idle
                # connections - release their resources.
                for ctx in self._connections.values():
                    ctx.close()
                log.debug("Transfer %s was canceled", self.transfer_id)
                return True

            log.debug("Canceling transfer %s ongoing operations",
                      self.transfer_id)
            # Cancel ongoing operations. This speeds up cancellation when
            # streaming lot of data. Operations will be canceled once they
            # complete the current I/O.
            for op in self._ongoing:
                op.cancel()

        if timeout:
            log.info("Waiting until transfer %s ongoing operations finish",
                     self.transfer_id)
            if not self._unused.wait(timeout):
                raise errors.TransferCancelTimeout(self.transfer_id)

            # Finished ongoing operations discover that the ticket was canceled
            # and close the connection. We need to release resources used by
            # idle connections.
            with self._lock:
                for ctx in self._connections.values():
                    ctx.close()

            log.info("Transfer %s was canceled", self.transfer_id)
            return True

        # The caller need to wait until ongoing operations finish by polling
        # the ticket "active" property. When the ticket becomes inactive,
        # caller must call again to delete the ticket.
        return False

    def __repr__(self):
        return ("<Ticket "
                "active={active!r} "
                "canceled={self._canceled} "
                "connections={connections} "
                "expires={self.expires!r} "
                "inactivity_timeout={self.inactivity_timeout} "
                "filename={self.filename!r} "
                "idle_time={self.idle_time} "
                "ops={self.ops!r} "
                "size={self.size!r} "
                "sparse={self.sparse!r} "
                "dirty={self.dirty!r} "
                "transfer_id={self.transfer_id!r} "
                "transferred={transferred!r} "
                "url={url!r} "
                "uuid={self.uuid!r} "
                "at {addr:#x}>"
                ).format(
                    active=self.active(),
                    addr=id(self),
                    connections=len(self._connections),
                    self=self,
                    transferred=self.transferred(),
                    url=urllib_parse.urlunparse(self.url)
                )


def _required(d, key, type):
    if key not in d:
        raise errors.MissingTicketParameter(key)
    return _validate(key, d[key], type)


def _optional(d, key, type, default=None):
    if key not in d:
        return default
    return _validate(key, d[key], type)


def _validate(key, value, type):
    if not isinstance(value, type):
        raise errors.InvalidTicketParameter(
            key, value, "expecting a {!r} value".format(type))
    return value


class Authorizer:

    def __init__(self, config):
        self._config = config
        self._tickets = {}

    def add(self, ticket_dict):
        """
        Add a ticket to the store.

        Raises errors.InvalidTicket if ticket dict is invalid.
        """
        ticket = Ticket(ticket_dict, self._config)
        self._tickets[ticket.uuid] = ticket

    def remove(self, ticket_id):
        try:
            ticket = self._tickets[ticket_id]
        except KeyError:
            log.debug("Ticket %s does not exist", ticket_id)
            return

        # Cancel the ticket and wait until the ticket is unused. Will raise
        # errors.TransferCancelTimeout if the ticket could not be canceled
        # within the timeout.
        if ticket.cancel(self._config.control.remove_timeout):
            # Ticket is unused now, so it is safe to remove it.
            del self._tickets[ticket_id]

    def clear(self):
        self._tickets.clear()

    def get(self, ticket_id):
        """
        Gets a ticket ID and returns the proper
        Ticket object from the tickets' cache.
        """
        return self._tickets[ticket_id]

    def authorize(self, ticket_id, op):
        """
        Authorizing a ticket operation
        """
        try:
            ticket = self._tickets[ticket_id]
        except KeyError:
            raise errors.AuthorizationError(
                "No such ticket {}".format(ticket_id))

        log.debug("AUTH op=%s transfer=%s", op, ticket.transfer_id)

        if ticket.canceled:
            raise errors.AuthorizationError(
                "Transfer={} was canceled".format(ticket.transfer_id))

        if ticket.expires <= util.monotonic_time():
            raise errors.AuthorizationError(
                "Transfer={} expired".format(ticket.transfer_id))

        if not ticket.may(op):
            raise errors.AuthorizationError(
                "Transfer={} forbids {}".format(ticket.transfer_id, op))

        return ticket
