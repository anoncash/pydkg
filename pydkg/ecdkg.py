import enum
import functools
import logging
import math

from py_ecc.secp256k1 import secp256k1
from sqlalchemy import types
from sqlalchemy.schema import Column, ForeignKey, UniqueConstraint
from sqlalchemy.orm import relationship

from . import db, util, networking
from .rpc_interface import ProtocolError

COMS_TIMEOUT = 10
THRESHOLD_FACTOR = .5
# NOTE: As soon as I end the python shell session that I created this in
#       and the RAM for that session gets reused, the scalar used to produce
#       this point probably won't come into existence again.
# TODO: reroll this point in dark ritual ala Zcash zkSNARK toxic waste thing
#       ... not that this parameter creates _much_ more security for this
#       protocol, but it's applicable and could be hilarious if you don't
#       believe the above note.
G2 = (0xb25b5ea8b8b230e5574fec0182e809e3455701323968c602ab56b458d0ba96bf,
      0x13edfe75e1c88e030eda220ffc74802144aec67c4e51cb49699d4401c122e19c)
util.validate_curve_point(G2)


def random_polynomial(order: int) -> tuple:
    return tuple(util.random_private_value() for _ in range(order))


def eval_polynomial(poly: tuple, x: int) -> int:
    return sum(c * pow(x, k, secp256k1.N) for k, c in enumerate(poly)) % secp256k1.N


def generate_public_shares(poly1, poly2):
    if len(poly1) != len(poly2):
        raise ValueError('polynomial lengths must match ({} != {})'.format(len(poly1), len(poly2)))

    return (secp256k1.add(secp256k1.multiply(secp256k1.G, a), secp256k1.multiply(G2, b)) for a, b in zip(poly1, poly2))


@enum.unique
class ECDKGPhase(enum.IntEnum):
    uninitialized = 0
    key_distribution = 1
    key_verification = 2
    key_check = 3
    key_generation = 4
    key_publication = 5
    complete = 6


class ECDKG(db.Base):
    __tablename__ = 'ecdkg'

    decryption_condition = Column(types.String(32), index=True, unique=True)
    phase = Column(types.Enum(ECDKGPhase), nullable=False, default=ECDKGPhase.uninitialized)
    threshold = Column(types.Integer)
    encryption_key = Column(db.CurvePoint)
    decryption_key = Column(db.PrivateValue)
    participants = relationship('ECDKGParticipant', back_populates='ecdkg')

    secret_poly1 = Column(db.Polynomial)
    secret_poly2 = Column(db.Polynomial)
    verification_points = Column(db.CurvePointTuple)
    encryption_key_part = Column(db.CurvePoint)

    @classmethod
    def get_or_create_by_decryption_condition(cls, decryption_condition: str) -> 'ECDKG':
        decryption_condition = util.normalize_decryption_condition(decryption_condition)
        ecdkg_obj = (
            db.Session
            .query(cls)
            .filter(cls.decryption_condition == decryption_condition)
            .scalar()
        )

        if ecdkg_obj is None:
            ecdkg_obj = cls(decryption_condition=decryption_condition)
            db.Session.add(ecdkg_obj)
            db.Session.commit()

        return ecdkg_obj

    async def run_until_phase(self, target_phase: ECDKGPhase):
        while self.phase < target_phase:
            logging.info('handling {} phase...'.format(self.phase.name))
            await getattr(self, 'handle_{}_phase'.format(self.phase.name))()

    async def handle_uninitialized_phase(self):
        for addr in networking.channels.keys():
            self.get_or_create_participant_by_address(addr)

        # everyone should on agree on participants
        self.threshold = math.ceil(THRESHOLD_FACTOR * (len(self.participants)+1))

        spoly1 = random_polynomial(self.threshold)
        spoly2 = random_polynomial(self.threshold)

        self.secret_poly1 = spoly1
        self.secret_poly2 = spoly2

        self.encryption_key_part = secp256k1.multiply(secp256k1.G, self.secret_poly1[0])

        self.verification_points = tuple(
            secp256k1.add(secp256k1.multiply(secp256k1.G, a), secp256k1.multiply(G2, b))
            for a, b in zip(spoly1, spoly2)
        )

        self.phase = ECDKGPhase.key_distribution
        db.Session.commit()

    async def handle_key_distribution_phase(self):
        global own_address

        signed_secret_shares = await networking.broadcast_jsonrpc_call_on_all_channels(
            'get_signed_secret_shares', self.decryption_condition)

        for participant in self.participants:
            address = participant.eth_address

            if address not in signed_secret_shares:
                logging.warning('missing share from address {:040x}'.format(address))
                continue

            (share1, share2), signature = signed_secret_shares[address]

            try:
                msg_bytes = (
                    self.decryption_condition.encode() +
                    util.address_to_bytes(own_address) +
                    b'SECRETSHARES' +
                    util.private_value_to_bytes(share1) +
                    util.private_value_to_bytes(share2)
                )

                recovered_address = util.address_from_message_and_signature(msg_bytes, signature)

            except ValueError as e:
                logging.warning('signature from address {:040x} could not be verified: {}'.format(address, e))
                continue

            if address != recovered_address:
                logging.warning(
                    'address of channel {:040x} does not match recovered address {:040x}'
                    .format(address, recovered_address)
                )
                continue

            participant.secret_share1 = share1
            participant.secret_share2 = share2
            participant.shares_signature = signature

        logging.info('set all secret shares')
        verification_points = await networking.broadcast_jsonrpc_call_on_all_channels(
            'get_verification_points', self.decryption_condition)

        for participant in self.participants:
            address = participant.eth_address
            if address in verification_points:
                participant.verification_points = tuple(tuple(
                    int(ptstr[i:i+64], 16) for i in (0, 64)) for ptstr in verification_points[address])
            else:
                logging.warning('missing verification_points from address {:040x}'.format(address))

        self.phase = ECDKGPhase.key_verification
        db.Session.commit()

    async def handle_key_verification_phase(self):
        global own_address

        for participant in self.participants:
            share1 = participant.secret_share1
            share2 = participant.secret_share2

            if share1 is not None and share2 is not None:
                vlhs = secp256k1.add(secp256k1.multiply(secp256k1.G, share1),
                                     secp256k1.multiply(G2, share2))
                vrhs = functools.reduce(
                    secp256k1.add,
                    (secp256k1.multiply(ps, pow(own_address, k, secp256k1.N))
                        for k, ps in enumerate(participant.verification_points)))

                if vlhs == vrhs:
                    continue

            participant.get_or_create_complaint_by_complainer_address(own_address)

        self.phase = ECDKGPhase.key_check
        db.Session.commit()

    async def handle_key_check_phase(self):
        complaints = await networking.broadcast_jsonrpc_call_on_all_channels(
            'get_complaints', self.decryption_condition)

        for participant in self.participants:
            complainer_address = participant.eth_address

            if complainer_address in complaints:
                # TODO: Add complaints and collect responses to complaints
                pass

        self.phase = ECDKGPhase.key_generation
        db.Session.commit()

    async def handle_key_generation_phase(self):
        encryption_key_parts = await networking.broadcast_jsonrpc_call_on_all_channels(
            'get_encryption_key_part', self.decryption_condition)

        for participant in self.participants:
            address = participant.eth_address
            if address in encryption_key_parts:
                ekp = tuple(int(encryption_key_parts[address][i:i+64], 16) for i in (0, 64))
                participant.encryption_key_part = ekp
            else:
                # TODO: this is supposed to be broadcast... maybe try getting it from other nodes instead?
                raise ProtocolError('missing encryption_key_part from address {:040x}'.format(address))

        self.encryption_key = functools.reduce(
            secp256k1.add,
            (p.encryption_key_part for p in self.participants),
            self.encryption_key_part
        )

        self.phase = ECDKGPhase.key_publication
        db.Session.commit()

    async def handle_key_publication_phase(self):
        await util.decryption_condition_satisfied(self.decryption_condition)

        dec_key_parts = await networking.broadcast_jsonrpc_call_on_all_channels(
            'get_decryption_key_part', self.decryption_condition)

        for p in self.participants:
            address = p.eth_address
            if address in dec_key_parts:
                p.decryption_key_part = int(dec_key_parts[address], 16)
            else:
                # TODO: switch to interpolation of secret shares if waiting doesn't work
                raise ProtocolError('missing decryption key part!')

        self.decryption_key = (
            sum(p.decryption_key_part for p in self.participants) +
            self.secret_poly1[0]
        ) % secp256k1.N

        self.phase = ECDKGPhase.complete
        db.Session.commit()

    def get_or_create_participant_by_address(self, address: int) -> 'ECDKGParticipant':
        participant = (
            db.Session
            .query(ECDKGParticipant)
            .filter(ECDKGParticipant.ecdkg_id == self.id,
                    ECDKGParticipant.eth_address == address)
            .scalar()
        )

        if participant is None:
            participant = ECDKGParticipant(ecdkg_id=self.id, eth_address=address)
            db.Session.add(participant)
            db.Session.commit()

        return participant

    def get_signed_secret_shares(self, address: int) -> ((int, int), 'rsv triplet'):
        global private_key

        secret_shares = (eval_polynomial(self.secret_poly1, address),
                         eval_polynomial(self.secret_poly2, address))

        msg_bytes = (
            self.decryption_condition.encode() +
            util.address_to_bytes(address) +
            b'SECRETSHARES' +
            util.private_value_to_bytes(secret_shares[0]) +
            util.private_value_to_bytes(secret_shares[1])
        )

        signature = util.sign_with_key(msg_bytes, private_key)

        return (secret_shares, signature)

    def get_complaints_by(self, address: int) -> dict:
        return (
            db.Session
            .query(ECDKGComplaint)
            .filter(  # ECDKGComplaint.participant.ecdkg_id == self.id,
                    ECDKGComplaint.complainer_address == address)
            .all()
        )

    def to_state_message(self) -> dict:
        global own_address

        msg = {'address': '{:040x}'.format(own_address)}

        for attr in ('decryption_condition', 'phase', 'threshold'):
            val = getattr(self, attr)
            if val is not None:
                msg[attr] = val

        msg['participants'] = {'{:040x}'.format(p.eth_address): p.to_state_message() for p in self.participants}

        for attr in ('encryption_key', 'encryption_key_part'):
            val = getattr(self, attr)
            if val is not None:
                msg[attr] = '{0[0]:064x}{0[1]:064x}'.format(val)

        vpts = self.verification_points
        if vpts is not None:
            msg['verification_points'] = tuple('{0[0]:064x}{0[1]:064x}'.format(pt) for pt in vpts)

        return msg


class ECDKGParticipant(db.Base):
    __tablename__ = 'ecdkg_participant'

    ecdkg_id = Column(types.Integer, ForeignKey('ecdkg.id'))
    ecdkg = relationship('ECDKG', back_populates='participants')
    eth_address = Column(db.EthAddress, index=True)

    encryption_key_part = Column(db.CurvePoint)
    decryption_key_part = Column(db.PrivateValue)
    verification_points = Column(db.CurvePointTuple)
    secret_share1 = Column(db.PrivateValue)
    secret_share2 = Column(db.PrivateValue)
    shares_signature = Column(db.Signature)

    complaints = relationship('ECDKGComplaint', back_populates='participant')

    __table_args__ = (UniqueConstraint('ecdkg_id', 'eth_address'),)

    def get_or_create_complaint_by_complainer_address(self, address: int) -> 'ECDKGComplaint':
        complaint = (
            db.Session
            .query(ECDKGComplaint)
            .filter(ECDKGComplaint.participant_id == self.id,
                    ECDKGComplaint.complainer_address == address)
            .scalar()
        )

        if complaint is None:
            complaint = ECDKGComplaint(participant_id=self.id, complainer_address=address)
            db.Session.add(complaint)
            db.Session.commit()

        return complaint

    def to_state_message(self, address: int = None) -> dict:
        msg = {}

        for attr in ('encryption_key_part', 'verification_points'):
            val = getattr(self, attr)
            if val is not None:
                msg[attr] = '{0[0]:064x}{0[1]:064x}'.format(val)

        return msg


class ECDKGComplaint(db.Base):
    __tablename__ = 'ecdkg_complaint'

    participant_id = Column(types.Integer, ForeignKey('ecdkg_participant.id'))
    participant = relationship('ECDKGParticipant', back_populates='complaints')
    complainer_address = Column(db.EthAddress, index=True)

    __table_args__ = (UniqueConstraint('participant_id', 'complainer_address'),)
