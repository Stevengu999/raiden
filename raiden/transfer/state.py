# -*- coding: utf-8 -*-
# pylint: disable=too-few-public-methods,too-many-arguments,too-many-instance-attributes
import random
from binascii import hexlify
from collections import namedtuple

from raiden.constants import UINT256_MAX, UINT64_MAX
from raiden.encoding.format import buffer_for
from raiden.encoding import messages
from raiden.transfer.architecture import State
from raiden.transfer.merkle_tree import merkleroot
from raiden.utils import lpex, pex, sha3, typing

CHANNEL_STATE_CLOSED = 'closed'
CHANNEL_STATE_CLOSING = 'waiting_for_close'
CHANNEL_STATE_OPENED = 'opened'
CHANNEL_STATE_SETTLED = 'settled'
CHANNEL_STATE_SETTLING = 'waiting_for_settle'
CHANNEL_STATE_UNUSABLE = 'channel_unusable'

CHANNEL_ALL_VALID_STATES = (
    CHANNEL_STATE_CLOSED,
    CHANNEL_STATE_CLOSING,
    CHANNEL_STATE_OPENED,
    CHANNEL_STATE_SETTLED,
    CHANNEL_STATE_SETTLING,
    CHANNEL_STATE_UNUSABLE,
)

CHANNEL_STATES_PRIOR_TO_CLOSED = (
    CHANNEL_STATE_OPENED,
    CHANNEL_STATE_CLOSING,
)

CHANNEL_AFTER_CLOSE_STATES = (
    CHANNEL_STATE_CLOSED,
    CHANNEL_STATE_SETTLING,
    CHANNEL_STATE_SETTLED,
)

NODE_NETWORK_UNKNOWN = 'unknown'
NODE_NETWORK_UNREACHABLE = 'unreachable'
NODE_NETWORK_REACHABLE = 'reachable'


def balanceproof_from_envelope(envelope_message):
    return BalanceProofSignedState(
        envelope_message.nonce,
        envelope_message.transferred_amount,
        envelope_message.locked_amount,
        envelope_message.locksroot,
        envelope_message.channel,
        envelope_message.message_hash,
        envelope_message.signature,
        envelope_message.sender,
    )


def lockstate_from_lock(lock):
    return HashTimeLockState(
        lock.amount,
        lock.expiration,
        lock.secrethash,
    )


def message_identifier_from_prng(prng):
    return prng.randint(0, UINT64_MAX)


class NodeState(State):
    """ Umbrella object that stores all the node state.
    For each registry smart contract there must be a payment network. Within the
    payment network the existing token networks and channels are registered.
    """

    __slots__ = (
        'queueids_to_queues',
        'pseudo_random_generator',
        'block_number',
        'identifiers_to_paymentnetworks',
        'nodeaddresses_to_networkstates',
        'payment_mapping',
    )

    def __init__(self, pseudo_random_generator: random.Random, block_number: typing.BlockNumber):
        if not isinstance(block_number, typing.T_BlockNumber):
            raise ValueError('block_number must be a block_number')

        self.pseudo_random_generator = pseudo_random_generator
        self.block_number = block_number
        self.queueids_to_queues = dict()
        self.identifiers_to_paymentnetworks = dict()
        self.nodeaddresses_to_networkstates = dict()
        self.payment_mapping = PaymentMappingState()

    def __repr__(self):
        return '<NodeState block:{} networks:{} qtd_transfers:{}>'.format(
            self.block_number,
            lpex(self.identifiers_to_paymentnetworks.keys()),
            len(self.payment_mapping.secrethashes_to_task),
        )

    def __eq__(self, other):
        return (
            isinstance(other, NodeState) and
            self.pseudo_random_generator == other.pseudo_random_generator and
            self.block_number == other.block_number and
            self.queueids_to_queues == other.queueids_to_queues and
            self.identifiers_to_paymentnetworks == other.identifiers_to_paymentnetworks and
            self.nodeaddresses_to_networkstates == other.nodeaddresses_to_networkstates and
            self.payment_mapping == other.payment_mapping
        )

    def __ne__(self, other):
        return not self.__eq__(other)


class PaymentNetworkState(State):
    """ Corresponds to a registry smart contract. """

    __slots__ = (
        'address',
        'tokenidentifiers_to_tokennetworks',
        'tokenaddresses_to_tokennetworks',
    )

    def __init__(
            self,
            address: typing.Address,
            token_network_list: typing.List['TokenNetworkState']):

        if not isinstance(address, typing.T_Address):
            raise ValueError('address must be an address instance')

        self.address = address
        self.tokenidentifiers_to_tokennetworks = {
            token_network.address: token_network
            for token_network in token_network_list
        }
        self.tokenaddresses_to_tokennetworks = {
            token_network.token_address: token_network
            for token_network in token_network_list
        }

    def __repr__(self):
        return '<PaymentNetworkState id:{}>'.format(pex(self.address))

    def __eq__(self, other):
        return (
            isinstance(other, PaymentNetworkState) and
            self.address == other.address and
            self.tokenaddresses_to_tokennetworks == other.tokenaddresses_to_tokennetworks and
            self.tokenidentifiers_to_tokennetworks == other.tokenidentifiers_to_tokennetworks
        )

    def __ne__(self, other):
        return not self.__eq__(other)


class TokenNetworkState(State):
    """ Corresponds to a channel manager smart contract. """

    __slots__ = (
        'address',
        'token_address',
        'network_graph',
        'channelidentifiers_to_channels',
        'partneraddresses_to_channels',
    )

    def __init__(
            self,
            address: typing.Address,
            token_address: typing.Address,
            network_graph: 'TokenNetworkGraphState',
            partner_channels: typing.List['NettingChannelState']):

        if not isinstance(address, typing.T_Address):
            raise ValueError('address must be an address instance')

        if not isinstance(token_address, typing.T_Address):
            raise ValueError('token_address must be an address instance')

        if not isinstance(network_graph, TokenNetworkGraphState):
            raise ValueError('network_graph must be a TokenNetworkGraphState instance')

        self.address = address
        self.token_address = token_address
        self.network_graph = network_graph

        self.channelidentifiers_to_channels = {
            channel.identifier: channel
            for channel in partner_channels
        }
        self.partneraddresses_to_channels = {
            channel.partner_state.address: channel
            for channel in partner_channels
        }

    def __repr__(self):
        return '<TokenNetworkState id:{} token:{}>'.format(
            pex(self.address),
            pex(self.token_address),
        )

    def __eq__(self, other):
        return (
            isinstance(other, TokenNetworkState) and
            self.address == other.address and
            self.token_address == other.token_address and
            self.network_graph == other.network_graph and
            self.channelidentifiers_to_channels == other.channelidentifiers_to_channels and
            self.partneraddresses_to_channels == other.partneraddresses_to_channels
        )

    def __ne__(self, other):
        return not self.__eq__(other)


# This is necessary for the routing only, maybe it should be transient state
# outside of the state tree.
class TokenNetworkGraphState(State):
    """ Stores the existing channels in the channel manager contract, used for
    route finding.
    """

    __slots__ = (
        'network',
    )

    def __init__(self, network):
        self.network = network

    def __repr__(self):
        return '<TokenNetworkGraphState>'

    def __eq__(self, other):
        return (
            isinstance(other, TokenNetworkGraphState) and
            self.network == other.network
        )

    def __ne__(self, other):
        return not self.__eq__(other)


class PaymentMappingState(State):
    """ Global map from secrethash to a transfer task.
    This mapping is used to quickly dispatch state changes by secrethash, for
    those that dont have a balance proof, e.g. SecretReveal.
    This mapping forces one task per secrethash, assuming that secrethash collision
    is unlikely. Features like token swaps, that span multiple networks, must
    be encapsulated in a single task to work with this structure.
    """

    # Because of retries, there may be multiple transfers for the same payment,
    # IOW there may be more than one task for the same transfer identifier. For
    # this reason the mapping uses the secrethash as key.
    #
    # Because token swaps span multiple token networks, the state of the
    # payment task is kept in this mapping, instead of inside an arbitrary
    # token network.
    __slots__ = (
        'secrethashes_to_task',
    )

    InitiatorTask = namedtuple('InitiatorTask', (
        'payment_network_identifier',
        'token_address',
        'manager_state',
    ))

    MediatorTask = namedtuple('MediatorTask', (
        'payment_network_identifier',
        'token_address',
        'mediator_state',
    ))

    TargetTask = namedtuple('TargetTask', (
        'payment_network_identifier',
        'token_address',
        'channel_identifier',
        'target_state',
    ))

    def __init__(self):
        self.secrethashes_to_task = dict()

    def __repr__(self):
        return '<PaymentMappingState qtd_transfers:{}>'.format(
            len(self.secrethashes_to_task)
        )

    def __eq__(self, other):
        return (
            isinstance(other, PaymentMappingState) and
            self.secrethashes_to_task == other.secrethashes_to_task
        )

    def __ne__(self, other):
        return not self.__eq__(other)


class RouteState(State):
    """ A possible route provided by a routing service.

    Args:
        node_address (address): The address of the next_hop.
        channel_identifier: The channel identifier.
    """

    __slots__ = (
        'node_address',
        'channel_identifier',
    )

    def __init__(self, node_address: typing.Address, channel_identifier):
        if not isinstance(node_address, typing.T_Address):
            raise ValueError('node_address must be an address instance')

        self.node_address = node_address
        self.channel_identifier = channel_identifier

    def __repr__(self):
        return '<RouteState hop:{node} channel:{channel}>'.format(
            node=pex(self.node_address),
            channel=pex(self.channel_identifier),
        )

    def __eq__(self, other):
        return (
            isinstance(other, RouteState) and
            self.node_address == other.node_address and
            self.channel_identifier == other.channel_identifier
        )

    def __ne__(self, other):
        return not self.__eq__(other)


class BalanceProofUnsignedState(State):
    """ Balance proof from the local node without the signature. """

    __slots__ = (
        'nonce',
        'transferred_amount',
        'locked_amount',
        'locksroot',
        'channel_address',
    )

    def __init__(
            self,
            nonce: int,
            transferred_amount: typing.TokenAmount,
            locked_amount: typing.TokenAmount,
            locksroot: typing.Keccak256,
            channel_address: typing.Address,
    ):

        if not isinstance(nonce, int):
            raise ValueError('nonce must be int')

        if not isinstance(transferred_amount, typing.T_TokenAmount):
            raise ValueError('transferred_amount must be a token_amount instance')

        if not isinstance(locked_amount, typing.T_TokenAmount):
            raise ValueError('locked_amount must be a token_amount instance')

        if not isinstance(locksroot, typing.T_Keccak256):
            raise ValueError('locksroot must be a keccak256 instance')

        if not isinstance(channel_address, typing.T_Address):
            raise ValueError('channel_address must be an address instance')

        if nonce <= 0:
            raise ValueError('nonce cannot be zero or negative')

        if nonce > UINT64_MAX:
            raise ValueError('nonce is too large')

        if transferred_amount < 0:
            raise ValueError('transferred_amount cannot be negative')

        if transferred_amount > UINT256_MAX:
            raise ValueError('transferred_amount is too large')

        if len(locksroot) != 32:
            raise ValueError('locksroot must have length 32')

        if len(channel_address) != 20:
            raise ValueError('channel is an invalid address')

        self.nonce = nonce
        self.transferred_amount = transferred_amount
        self.locked_amount = locked_amount
        self.locksroot = locksroot
        self.channel_address = channel_address

    def __repr__(self):
        return (
            '<'
            'BalanceProofUnsignedState nonce:{} transferred_amount:{} '
            'locked_amount:{} locksroot:{} channel_address:{}'
            '>'
        ).format(
            self.nonce,
            self.transferred_amount,
            self.locked_amount,
            pex(self.locksroot),
            pex(self.channel_address),
        )

    def __eq__(self, other):
        return (
            isinstance(other, BalanceProofUnsignedState) and
            self.nonce == other.nonce and
            self.transferred_amount == other.transferred_amount and
            self.locked_amount == other.locked_amount and
            self.locksroot == other.locksroot and
            self.channel_address == other.channel_address
        )

    def __ne__(self, other):
        return not self.__eq__(other)


class BalanceProofSignedState(State):
    """ Proof of a channel balance that can be used on-chain to resolve
    disputes.
    """

    __slots__ = (
        'nonce',
        'transferred_amount',
        'locked_amount',
        'locksroot',
        'channel_address',
        'message_hash',
        'signature',
        'sender',
    )

    def __init__(
            self,
            nonce: int,
            transferred_amount: typing.TokenAmount,
            locked_amount: typing.TokenAmount,
            locksroot: typing.Keccak256,
            channel_address: typing.Address,
            message_hash: typing.Keccak256,
            signature: typing.Signature,
            sender: typing.Address,
    ):

        if not isinstance(nonce, int):
            raise ValueError('nonce must be int')

        if not isinstance(transferred_amount, typing.T_TokenAmount):
            raise ValueError('transferred_amount must be a token_amount instance')

        if not isinstance(locked_amount, typing.T_TokenAmount):
            raise ValueError('locked_amount must be a token_amount instance')

        if not isinstance(locksroot, typing.T_Keccak256):
            raise ValueError('locksroot must be a keccak256 instance')

        if not isinstance(channel_address, typing.T_Address):
            raise ValueError('channel_address must be an address instance')

        if not isinstance(message_hash, typing.T_Keccak256):
            raise ValueError('message_hash must be a keccak256 instance')

        if not isinstance(signature, typing.T_Signature):
            raise ValueError('signature must be a signature instance')

        if not isinstance(sender, typing.T_Address):
            raise ValueError('sender must be an address instance')

        if nonce <= 0:
            raise ValueError('nonce cannot be zero or negative')

        if nonce > UINT64_MAX:
            raise ValueError('nonce is too large')

        if transferred_amount < 0:
            raise ValueError('transferred_amount cannot be negative')

        if transferred_amount > UINT256_MAX:
            raise ValueError('transferred_amount is too large')

        if len(locksroot) != 32:
            raise ValueError('locksroot must have length 32')

        if len(channel_address) != 20:
            raise ValueError('channel is an invalid address')

        if len(message_hash) != 32:
            raise ValueError('message_hash is an invalid hash')

        if len(signature) != 65:
            raise ValueError('signature is an invalid signature')

        self.nonce = nonce
        self.transferred_amount = transferred_amount
        self.locked_amount = locked_amount
        self.locksroot = locksroot
        self.channel_address = channel_address
        self.message_hash = message_hash
        self.signature = signature
        self.sender = sender

    def __repr__(self):
        return (
            '<'
            'BalanceProofSignedState nonce:{} transferred_amount:{} '
            'locked_amount:{} locksroot:{} channel_address:{} message_hash:{}'
            'signature:{} sender:{}'
            '>'
        ).format(
            self.nonce,
            self.transferred_amount,
            self.locked_amount,
            pex(self.locksroot),
            pex(self.channel_address),
            pex(self.message_hash),
            pex(self.signature),
            pex(self.sender),
        )

    def __eq__(self, other):
        return (
            isinstance(other, BalanceProofSignedState) and
            self.nonce == other.nonce and
            self.transferred_amount == other.transferred_amount and
            self.locked_amount == other.locked_amount and
            self.locksroot == other.locksroot and
            self.channel_address == other.channel_address and
            self.message_hash == other.message_hash and
            self.signature == other.signature and
            self.sender == other.sender
        )

    def __ne__(self, other):
        return not self.__eq__(other)


class HashTimeLockState(State):
    """ Represents a hash time lock. """

    __slots__ = (
        'amount',
        'expiration',
        'secrethash',
        'encoded',
        'lockhash',
    )

    def __init__(
            self,
            amount: typing.TokenAmount,
            expiration: typing.BlockNumber,
            secrethash: typing.Keccak256):

        if not isinstance(amount, typing.T_TokenAmount):
            raise ValueError('amount must be a token_amount instance')

        if not isinstance(expiration, typing.T_BlockNumber):
            raise ValueError('expiration must be a block_number instance')

        if not isinstance(secrethash, typing.T_Keccak256):
            raise ValueError('secrethash must be a keccak256 instance')

        packed = messages.Lock(buffer_for(messages.Lock))
        packed.amount = amount
        packed.expiration = expiration
        packed.secrethash = secrethash
        encoded = bytes(packed.data)

        self.amount = amount
        self.expiration = expiration
        self.secrethash = secrethash
        self.encoded = encoded
        self.lockhash = sha3(encoded)

    def __repr__(self):
        return '<HashTimeLockState amount:{} expiration:{} secrethash:{}>'.format(
            self.amount,
            self.expiration,
            pex(self.secrethash),
        )

    def __eq__(self, other):
        return (
            isinstance(other, HashTimeLockState) and
            self.amount == other.amount and
            self.expiration == other.expiration and
            self.secrethash == other.secrethash
        )

    def __ne__(self, other):
        return not self.__eq__(other)

    def __hash__(self):
        return self.lockhash


class UnlockPartialProofState(State):
    """ Stores the lock along with its unlocking secret. """

    __slots__ = (
        'lock',
        'secret',
    )

    def __init__(self, lock: HashTimeLockState, secret: typing.Secret):
        if not isinstance(lock, HashTimeLockState):
            raise ValueError('lock must be a HashTimeLockState instance')

        if not isinstance(secret, typing.T_Secret):
            raise ValueError('secret must be a secret instance')

        self.lock = lock
        self.secret = secret

    def __repr__(self):
        return '<UnlockPartialProofState lock:{}>'.format(
            self.lock,
        )

    def __eq__(self, other):
        return (
            isinstance(other, UnlockPartialProofState) and
            self.lock == other.lock and
            self.secret == other.secret
        )

    def __ne__(self, other):
        return not self.__eq__(other)


class UnlockProofState(State):
    """ An unlock proof for a given lock. """

    __slots__ = (
        'merkle_proof',
        'lock_encoded',
        'secret',
    )

    def __init__(
            self,
            merkle_proof: typing.List[typing.Keccak256],
            lock_encoded,
            secret: typing.Secret):

        if not isinstance(secret, typing.T_Secret):
            raise ValueError('secret must be a secret instance')

        self.merkle_proof = merkle_proof
        self.lock_encoded = lock_encoded
        self.secret = secret

    def __repr__(self):
        full_proof = [hexlify(entry) for entry in self.merkle_proof]
        return f'<UnlockProofState proof:{full_proof} lock:{hexlify(self.lock_encoded)}>'

    def __eq__(self, other):
        return (
            isinstance(other, UnlockProofState) and
            self.merkle_proof == other.merkle_proof and
            self.lock_encoded == other.lock_encoded and
            self.secret == other.secret
        )

    def __ne__(self, other):
        return not self.__eq__(other)


class TransactionExecutionStatus(State):
    """ Represents the status of a transaction. """
    SUCCESS = 'success'
    FAILURE = 'failure'
    VALID_RESULT_VALUES = (
        SUCCESS,
        FAILURE,
        None,
    )

    def __init__(
            self,
            started_block_number: typing.Optional[typing.BlockNumber],
            finished_block_number: typing.Optional[typing.BlockNumber],
            result):

        # started_block_number is set for the node that sent the transaction,
        # None otherwise
        if not (started_block_number is None or isinstance(started_block_number, int)):
            raise ValueError('started_block_number must be None or a block_number')

        if not (finished_block_number is None or isinstance(finished_block_number, int)):
            raise ValueError('finished_block_number must be None or a block_number')

        if result not in self.VALID_RESULT_VALUES:
            raise ValueError('result must be one of {}'.format(
                ','.join(self.VALID_RESULT_VALUES),
            ))

        self.started_block_number = started_block_number
        self.finished_block_number = finished_block_number
        self.result = result

    def __repr__(self):
        return '<TransactionExecutionStatus started:{} finished:{} result:{}>'.format(
            self.started_block_number,
            self.finished_block_number,
            self.result,
        )

    def __eq__(self, other):
        return (
            isinstance(other, TransactionExecutionStatus) and
            self.started_block_number == other.started_block_number and
            self.finished_block_number == other.finished_block_number and
            self.result == other.result
        )

    def __ne__(self, other):
        return not self.__eq__(other)


class MerkleTreeState(State):
    def __init__(self, layers):
        self.layers = layers

    def __repr__(self):
        return '<MerkleTreeState root:{}>'.format(
            pex(merkleroot(self)),
        )

    def __eq__(self, other):
        return (
            isinstance(other, MerkleTreeState) and
            self.layers == other.layers
        )

    def __ne__(self, other):
        return not self.__eq__(other)


class NettingChannelEndState(State):
    """ The state of one of the nodes in a two party netting channel. """

    __slots__ = (
        'address',
        'contract_balance',
        'secrethashes_to_lockedlocks',
        'secrethashes_to_unlockedlocks',
        'merkletree',
        'balance_proof',
    )

    def __init__(self, address: typing.Address, balance: typing.TokenAmount):
        if not isinstance(address, typing.T_Address):
            raise ValueError('address must be an address instance')

        if not isinstance(balance, typing.T_TokenAmount):
            raise ValueError('balance must be a token_amount isinstance')

        self.address = address
        self.contract_balance = balance

        self.secrethashes_to_lockedlocks = dict()
        self.secrethashes_to_unlockedlocks = dict()
        self.merkletree = EMPTY_MERKLE_TREE
        self.balance_proof = None

    def __repr__(self):
        return '<NettingChannelEndState address:{} contract_balance:{} merkletree:{}>'.format(
            pex(self.address),
            self.contract_balance,
            self.merkletree,
        )

    def __eq__(self, other):
        return (
            isinstance(other, NettingChannelEndState) and
            self.address == other.address and
            self.contract_balance == other.contract_balance and
            self.secrethashes_to_lockedlocks == other.secrethashes_to_lockedlocks and
            self.secrethashes_to_unlockedlocks == other.secrethashes_to_unlockedlocks and
            self.merkletree == other.merkletree and
            self.balance_proof == other.balance_proof
        )

    def __ne__(self, other):
        return not self.__eq__(other)


class NettingChannelState(State):
    """ The state of a netting channel. """

    __slots__ = (
        'identifier',
        'our_state',
        'partner_state',
        'token_address',
        'reveal_timeout',
        'settle_timeout',
        'deposit_transaction_queue',
        'open_transaction',
        'close_transaction',
        'settle_transaction',
    )

    def __init__(
            self,
            identifier,
            token_address: typing.Address,
            reveal_timeout: typing.BlockNumber,
            settle_timeout: typing.BlockNumber,
            our_state: NettingChannelEndState,
            partner_state: NettingChannelEndState,
            open_transaction: TransactionExecutionStatus,
            close_transaction: typing.Optional[TransactionExecutionStatus],
            settle_transaction: typing.Optional[TransactionExecutionStatus],
    ):

        if reveal_timeout >= settle_timeout:
            raise ValueError('reveal_timeout must be smaller than settle_timeout')

        if not isinstance(reveal_timeout, int) or reveal_timeout <= 0:
            raise ValueError('reveal_timeout must be a positive integer')

        if not isinstance(settle_timeout, int) or settle_timeout <= 0:
            raise ValueError('settle_timeout must be a positive integer')

        if not isinstance(open_transaction, TransactionExecutionStatus):
            raise ValueError('open_transaction must be a TransactionExecutionStatus instance')

        if open_transaction.result != TransactionExecutionStatus.SUCCESS:
            raise ValueError(
                'Cannot create a NettingChannelState with a non sucessfull open_transaction'
            )

        valid_close_transaction = (
            close_transaction is None or
            isinstance(close_transaction, TransactionExecutionStatus)
        )
        if not valid_close_transaction:
            raise ValueError('close_transaction must be a TransactionExecutionStatus instance')

        valid_settle_transaction = (
            settle_transaction is None or
            isinstance(settle_transaction, TransactionExecutionStatus)
        )
        if not valid_settle_transaction:
            raise ValueError(
                'settle_transaction must be a TransactionExecutionStatus instance or None'
            )

        self.identifier = identifier
        self.token_address = token_address
        self.reveal_timeout = reveal_timeout
        self.settle_timeout = settle_timeout
        self.our_state = our_state
        self.partner_state = partner_state
        self.deposit_transaction_queue = list()
        self.open_transaction = open_transaction
        self.close_transaction = close_transaction
        self.settle_transaction = settle_transaction

    def __repr__(self):
        return '<NettingChannelState id:{} opened:{} closed:{} settled:{}>'.format(
            pex(self.identifier),
            self.open_transaction,
            self.close_transaction,
            self.settle_transaction,
        )

    def __eq__(self, other):
        return (
            isinstance(other, NettingChannelState) and
            self.identifier == other.identifier and
            self.our_state == other.our_state and
            self.partner_state == other.partner_state and
            self.token_address == other.token_address and
            self.reveal_timeout == other.reveal_timeout and
            self.settle_timeout == other.settle_timeout and
            self.deposit_transaction_queue == other.deposit_transaction_queue and
            self.open_transaction == other.open_transaction and
            self.close_transaction == other.close_transaction and
            self.settle_transaction == other.settle_transaction
        )

    def __ne__(self, other):
        return not self.__eq__(other)


class TransactionChannelNewBalance(State):
    def __init__(
            self,
            participant_address: typing.Address,
            contract_balance: typing.TokenAmount,
            deposit_block_number: typing.BlockNumber,
    ):
        if not isinstance(participant_address, typing.T_Address):
            raise ValueError('participant_address must be of type address')

        if not isinstance(contract_balance, typing.T_BlockNumber):
            raise ValueError('contract_balance must be of type block_number')

        self.participant_address = participant_address
        self.contract_balance = contract_balance
        self.deposit_block_number = deposit_block_number

    def __repr__(self):
        return '<TransactionChannelNewBalance participant:{} balance:{} at_block:{}>'.format(
            pex(self.participant_address),
            self.contract_balance,
            self.deposit_block_number,
        )

    def __eq__(self, other):
        return (
            isinstance(other, TransactionChannelNewBalance) and
            self.participant_address == other.participant_address and
            self.contract_balance == other.contract_balance and
            self.deposit_block_number == other.deposit_block_number
        )

    def __ne__(self, other):
        return not self.__eq__(other)


EMPTY_MERKLE_ROOT = b'\x00' * 32
EMPTY_MERKLE_TREE = MerkleTreeState([
    [],                   # the leaves are empty
    [EMPTY_MERKLE_ROOT],  # the root is the constant 0
])
