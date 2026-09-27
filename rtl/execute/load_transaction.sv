`default_nettype none
module load_transaction (
  input wire clk_i, rst_i, kill_i, launch_i,
  output wire ready_o,
  input wire [12:0] id_i,
  input wire [31:0] address_i,
  input wire [1:0] size_i,
  input wire unsigned_i, uncached_i, head_only_i, fault_i,
  input wire [31:0] cause_i, trap_value_i,
  input wire issue_allowed_i, head_valid_i,
  input wire [12:0] head_id_i,
  output wire request_valid_o,
  input wire request_ready_i,
  output memory_protocol_pkg::mem_request_t request_o,
  input wire response_valid_i,
  output wire response_ready_o,
  input memory_protocol_pkg::mem_response_t response_i,
  output wire valid_o,
  input wire take_i,
  output wire [12:0] id_o, owner_id_o,
  output wire [31:0] address_o, data_o, cause_o, trap_value_o,
  output wire fault_o, busy_o, irrevocable_o,
  output logic fatal_o
);
  import memory_protocol_pkg::*;
  typedef enum logic [2:0] {EMPTY, WAIT_ISSUE, REQUEST, RESPONSE, RESULT} state_e;
  state_e state_q;
  logic [12:0] owner_q;
  logic [31:0] address_q, data_q, cause_q, trap_value_q;
  logic [1:0] size_q;
  logic unsigned_q, uncached_q, head_only_q, fault_q, discard_q;
  logic [3:0] transaction_q;
  wire active = !rst_i && !fatal_o;
  wire [31:0] read_mask = (size_q == 0 ? 32'hff : size_q == 1 ? 32'hffff : 32'hffffffff)
    << {address_q[1:0], 3'b000};
  wire malformed = response_valid_i && (state_q != RESPONSE
    || response_i.transaction_id != transaction_q
    || !(response_i.status inside {MEM_STATUS_OK, MEM_STATUS_ACCESS_FAULT})
    || (uncached_q ? response_i.line_read_data != 0 : response_i.uncached_read_data != 0)
    || (uncached_q && (response_i.uncached_read_data & ~read_mask) != 0)
    || (response_i.status != MEM_STATUS_OK
        && (response_i.line_read_data != 0 || response_i.uncached_read_data != 0)));
  wire [31:0] word_data = uncached_q ? response_i.uncached_read_data
    : response_i.line_read_data[address_q[4:2]*32 +: 32];
  wire [31:0] shifted = word_data >> {address_q[1:0], 3'b000};
  logic [31:0] extended;

  assign ready_o = active && !kill_i && state_q == EMPTY;
  assign busy_o = active && state_q != EMPTY;
  assign owner_id_o = busy_o ? owner_q : 13'd0;
  assign irrevocable_o = busy_o && uncached_q && !fault_q
    && (state_q == REQUEST || state_q == RESPONSE || state_q == RESULT);
  assign request_valid_o = active && state_q == REQUEST;
  assign response_ready_o = active && state_q == RESPONSE;
  assign valid_o = active && !kill_i && state_q == RESULT;
  assign id_o = valid_o ? owner_q : 13'd0;
  assign address_o = valid_o ? address_q : 32'd0;
  assign data_o = valid_o ? data_q : 32'd0;
  assign fault_o = valid_o && fault_q;
  assign cause_o = valid_o ? cause_q : 32'd0;
  assign trap_value_o = valid_o ? trap_value_q : 32'd0;
  always_comb begin
    request_o = '0;
    if (request_valid_o) begin
      request_o.transaction_id = transaction_q;
      request_o.uncached = uncached_q;
      request_o.address = uncached_q ? address_q : {address_q[31:5], 5'd0};
      if (uncached_q) request_o.uncached_size = mem_uncached_size_e'(size_q);
    end
    case (size_q)
      0: extended = {{24{!unsigned_q && shifted[7]}}, shifted[7:0]};
      1: extended = {{16{!unsigned_q && shifted[15]}}, shifted[15:0]};
      default: extended = shifted;
    endcase
  end
  always_ff @(posedge clk_i) begin
    if (rst_i) begin
      state_q <= EMPTY;
      transaction_q <= 0;
      discard_q <= 0;
      fatal_o <= 0;
    end else if (!fatal_o) begin
      if (malformed) fatal_o <= 1;
      case (state_q)
        EMPTY: if (launch_i) begin
          owner_q <= id_i;
          address_q <= address_i;
          size_q <= size_i;
          unsigned_q <= unsigned_i;
          uncached_q <= uncached_i;
          head_only_q <= head_only_i;
          fault_q <= fault_i;
          cause_q <= fault_i ? cause_i : 32'd0;
          trap_value_q <= fault_i ? trap_value_i : 32'd0;
          data_q <= 0;
          discard_q <= 0;
          state_q <= fault_i ? RESULT : WAIT_ISSUE;
        end
        WAIT_ISSUE: if (!kill_i && issue_allowed_i
          && (!(head_only_q || uncached_q) || (head_valid_i && head_id_i == owner_q)))
          state_q <= REQUEST;
        REQUEST: if (request_ready_i) state_q <= RESPONSE;
        RESPONSE: if (response_valid_i && !malformed) begin
          transaction_q <= transaction_q + 1'b1;
          discard_q <= 0;
          if (discard_q || kill_i) state_q <= EMPTY;
          else begin
            fault_q <= response_i.status == MEM_STATUS_ACCESS_FAULT;
            cause_q <= response_i.status == MEM_STATUS_ACCESS_FAULT ? 32'd5 : 32'd0;
            trap_value_q <= response_i.status == MEM_STATUS_ACCESS_FAULT ? address_q : 32'd0;
            data_q <= response_i.status == MEM_STATUS_OK ? extended : 32'd0;
            state_q <= RESULT;
          end
        end
        RESULT: if (take_i) state_q <= EMPTY;
        default: ;
      endcase
      // Cancellation drains offered/accepted reads before releasing the ID.
      if (kill_i) begin
        if (state_q == REQUEST || state_q == RESPONSE) begin
          if (!(state_q == RESPONSE && response_valid_i && !malformed)) discard_q <= 1;
        end else state_q <= EMPTY;
      end
    end
  end
`ifndef SYNTHESIS
  always_ff @(posedge clk_i) begin
    if (!rst_i) begin
      assert (!launch_i || ready_o) else $fatal(1, "LOAD_TX_CAPACITY");
      assert (!take_i || valid_o) else $fatal(1, "LOAD_TX_TAKE");
      assert (!kill_i || !irrevocable_o) else $fatal(1, "LOAD_TX_MMIO_KILL");
      if (launch_i) begin
        assert (size_i <= 2 && !(size_i == 2 && unsigned_i))
          else $fatal(1, "LOAD_TX_SIZE");
        assert (fault_i || ((size_i != 1 || !address_i[0])
          && (size_i != 2 || address_i[1:0] == 0))) else $fatal(1, "LOAD_TX_ALIGNMENT");
        assert (!fault_i || ((cause_i == 4 || cause_i == 5) && trap_value_i == address_i))
          else $fatal(1, "LOAD_TX_FAULT");
      end
    end
  end
`endif
endmodule
`default_nettype wire
